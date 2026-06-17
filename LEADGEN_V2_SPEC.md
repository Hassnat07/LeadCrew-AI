# LeadCrew AI v2 — Audit-First Lead Generation Spec

Spec for upgrading the existing CrewAI lead-gen project (`agents.py`, `app.py`,
`website_auditor.py`, `email_sender.py`, `database.py`). Hand this file to the
coding agent and implement phase by phase.

---

## 1. Why we are changing the architecture

**Current flow (v1):** LLM researcher picks ~5 companies → auditor checks them → copywriter emails them.

**Problem:** companies are chosen *before* we know if they have problems. When the audit
comes back clean, the agent invents weak reasons ("no contact form", "no chatbot").
Result: leads with no real pain → emails with no real hook → no clients.

**New flow (v2 — audit-first funnel):**

```
STAGE 1  HARVEST   (pure Python, no LLM)
         Serper Places API → 100–300 businesses per run
         (niche × city, e.g. "dentists in Manchester")
                 │
STAGE 2  AUDIT    (pure Python, no LLM)
         Deep automated audit of EVERY harvested business
         15+ pain-point checks (see §4)
                 │
STAGE 3  SCORE    (pure Python, no LLM)
         Weighted pain score 0–100. Keep only score ≥ 40
         AND business is "alive" (reviews ≥ 20 OR rating ≥ 4.0)
                 │
STAGE 4  ENRICH   (scraper + optional 1 cheap LLM call)
         Find published email + owner name (reuse existing
         6-step scraping logic from agents.py)
                 │
STAGE 5  WRITE    (LLM — the ONLY heavy LLM stage)
         One personalized email per qualified lead,
         citing the top 2 scored issues with exact numbers
                 │
STAGE 6  SEND + LOG
         Existing email_sender.py + leads_sent.db
         (dedup, 150/day cap, verification — keep as-is)
```

Key principle: **LLM only touches leads that already passed qualification.**
Everything before Stage 5 is deterministic Python — cheap, fast, repeatable, debuggable.

---

## 2. Stage 1 — Harvester (`harvester.py`, new file)

Use the **Serper Places endpoint** (same API key already in the project):

```python
import requests

def harvest(niche: str, city: str, api_key: str) -> list[dict]:
    resp = requests.post(
        "https://google.serper.dev/places",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": f"{niche} in {city}", "num": 100},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("places", [])
```

Each place typically includes: `title`, `address`, `website` (may be missing),
`phoneNumber`, `rating`, `ratingCount`, `category`. Verify exact field names
against a live response before relying on them.

Requirements:
- Accept a list of (niche, city) pairs from config; iterate and merge results.
- Deduplicate by website domain AND by phone number (same business appears
  under multiple queries).
- Skip businesses already in `leads_sent.db` (add a `seen_businesses` table —
  see §8 — so we never re-audit the same place twice).
- **Businesses with NO website at all are not discarded** — they are flagged
  `no_website=True` and routed to a separate "needs a website" campaign
  (highest-value lead type of all).

---

## 3. Stage 2 — Deep audit (extend `website_auditor.py`)

Keep everything that exists (PageSpeed scores, FCP/LCP, chatbot signatures,
viewport, outdated tech, contact form, CTA). **Add the following checks:**

| # | Check | How to detect | Why a client pays for it |
|---|-------|---------------|--------------------------|
| 1 | No SSL / cert problems | `requests.get("https://...")` fails or redirects to http; check cert expiry via `ssl` module | Browser shows "Not Secure" — kills trust instantly |
| 2 | Site completely down / 404 | Status code ≥ 400 or connection error on both www/non-www | Paying for a dead site |
| 3 | Stale copyright year | Regex `©\s*(19|20)\d{2}` in footer; flag if ≤ current_year − 2 | Visible proof site is abandoned |
| 4 | Missing/short title tag | `<title>` absent or < 15 chars | Basic SEO failure — invisible on Google |
| 5 | Missing meta description | No `<meta name="description">` | Same — easy, provable SEO pitch |
| 6 | Missing H1 | No `<h1>` on homepage | SEO + structure pitch |
| 7 | No schema markup | No `application/ld+json` and no `itemscope` | "Google can't read your business info" |
| 8 | No analytics/pixel | No gtag/GA/GTM/Meta-pixel script patterns | "You're flying blind — no data on visitors" |
| 9 | Broken internal links | Sample up to 15 internal `<a href>`, HEAD-request each, count ≥400s | Concrete, screenshot-able defect |
| 10 | No online booking | For service niches: no Calendly/booksy/fresha/setmore/acuity/"book online" patterns | Direct revenue argument — perfect for AI-booking-bot service |
| 11 | Huge page weight | PageSpeed `total-byte-weight` > 5 MB | Explains the slow score in plain words |
| 12 | Not mobile responsive | Existing viewport check + PageSpeed mobile < 50 | Most local traffic is mobile |
| 13 | Site builder fingerprint | Detect Wix/old WordPress (readme.html, /wp-content + generator meta with version ≤ 5), Squarespace, GoDaddy builder | "Stuck on a template that can't grow" |
| 14 | Email on free domain | Contact email is @gmail/@yahoo/@hotmail while business has a domain | Cheap, instant credibility pitch |
| 15 | Socials linked but dead | Social links present but pages 404 | Abandonment signal |

Implementation notes:
- Each check returns `(triggered: bool, evidence: str)` — evidence is a short
  human-readable fact with a number where possible, e.g.
  `"LCP is 7.2s (Google recommends < 2.5s)"`. These evidence strings flow
  straight into the email later. **Never store a vague reason.**
- Wrap every check in try/except; a failed check is `triggered=False`, never a crash.
- Run audits concurrently (`concurrent.futures.ThreadPoolExecutor`, max ~8
  workers) — 200 sites must finish in minutes, not hours.
- PageSpeed API is the slow/rate-limited part: only call it for sites that
  survive the cheap HTML checks with at least 1 issue, OR sample it; cache
  results in the DB so a site is never PageSpeed-tested twice in 30 days.

---

## 4. Stage 3 — Scoring (`scoring.py`, new file)

```python
WEIGHTS = {
    "no_website":        100,  # auto-qualify, separate campaign
    "site_down":          90,
    "no_ssl":             35,
    "mobile_score_lt_40": 30,
    "mobile_score_lt_60": 18,
    "lcp_gt_4s":          15,
    "not_mobile_ready":   25,
    "no_booking_system":  22,   # only for service niches
    "stale_copyright":    15,
    "broken_links":       15,
    "no_analytics":       12,
    "missing_title_meta": 12,
    "no_schema":           8,
    "outdated_tech":      12,
    "old_site_builder":   10,
    "free_email_domain":  10,
    "no_chatbot":          5,   # demoted: weak signal, tie-breaker only
    "no_contact_form":     5,   # demoted: weak signal, tie-breaker only
}
```

Rules:
- `pain_score = min(100, sum of triggered weights)`
- **Qualification threshold: pain_score ≥ 40.** Below that, the business goes
  to `seen_businesses` as `disqualified` and is never emailed.
- **Success filter (they must be able to pay):** `ratingCount ≥ 20` OR
  `rating ≥ 4.0` OR multiple locations. A struggling business with a bad
  website is not a client; a busy business with a bad website is.
- Output per lead: score + ordered list of `(issue, weight, evidence)` —
  the top 2 become the email hook.
- Note how `no_chatbot` / `no_contact_form` are now worth 5 points each:
  they can no longer qualify a lead on their own. That is the direct fix
  for the "weak reason" problem.

---

## 5. Stage 4 — Contact enrichment

Reuse the existing 6-step scraping ladder from `agents.py` (homepage footer →
/contact → /contact-us → /about → Google search → LinkedIn), but implement it
as plain Python where possible (steps 1–4 are just requests + BeautifulSoup +
mailto/tel regex). Only fall back to an LLM-with-search-tool call for the
hard cases (steps 5–6). Keep the existing rule: **never construct/guess emails.**

For `no_website=True` leads: phone number from the Places result is the
contact channel — export them to a separate WhatsApp/call list instead of email.

---

## 6. Stage 5 — Email writing (slim down the crew)

Replace the 3-agent crew with **one copywriter agent (or a single direct LLM
call per lead — simpler and cheaper than CrewAI for this step).**

Input per lead: business name, niche, city, rating/review count, top-2 issues
with evidence strings, sender details, service offered.

Email rules (keep from v1, they're good):
- Subject references the #1 issue with a real number.
- Sentence 1: the specific finding. Sentence 2–3: business cost in plain words
  ("at 7 seconds to load, most phone visitors leave before they see your
  prices"). Then one line on the fix, soft CTA, < 160 words.
- New: mention one *positive* fact (e.g. "you have 4.8★ across 240 reviews")
  before the problem — proves the email is genuinely about them, lifts reply
  rate, and frames the pitch as "your site is the only weak link".

Optional upgrade: attach an auto-generated one-page audit snapshot
(screenshot of the PageSpeed gauge via Playwright, or a simple HTML→PDF
scorecard). "Free audit attached" massively outperforms text-only cold email.

---

## 7. Stage 6 — Sending (keep, small additions)

`email_sender.py` + `leads_sent.db` already handle verification, dedup, and
the 150/day cap. Add:
- Per-domain throttle (max 1 email per company domain, already enforced by
  the unique index — keep).
- Send window randomization (gaps of 3–10 min) and a daily warm-up ramp if
  the sending domain is new.
- 3-day and 7-day follow-up sequences for non-repliers (new `followups`
  table; follow-up #1 re-states the #1 issue + one new finding).

---

## 8. Database additions (`database.py`)

```sql
CREATE TABLE IF NOT EXISTS seen_businesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, domain TEXT, phone TEXT,
    niche TEXT, city TEXT,
    rating REAL, rating_count INTEGER,
    pain_score INTEGER,
    issues_json TEXT,          -- [(issue, weight, evidence), ...]
    status TEXT,               -- harvested | disqualified | qualified | emailed | replied
    audited_at TEXT,
    UNIQUE(domain), UNIQUE(phone)
);
```

This table is the heart of daily automation: every run skips anything already
seen, so the system never wastes an audit or burns a lead twice.

---

## 9. Daily automation

- `run_daily.py`: takes today's (niche, city) pair from a rotating config list
  (e.g. 10 niches × 20 cities = 200 unique days of inventory), runs
  Stage 1→6 end-to-end, logs a summary (harvested / audited / qualified /
  emailed counts).
- Schedule: Windows Task Scheduler locally, or a GitHub Actions cron
  (the repo + Vercel pipeline already exists, so Actions is the natural fit) —
  note the SQLite DB then needs to live somewhere persistent (commit it back,
  or move to the existing Neon Postgres).
- Keep the Streamlit app as the dashboard: qualified-leads table sorted by
  pain score, evidence per lead, generated email preview, approve/send button.
  Manual approval first; switch to full-auto only after deliverability and
  reply quality are proven.

---

## 10. Build order

1. `harvester.py` + `seen_businesses` table — verify Places output fields live.
2. New audit checks in `website_auditor.py` (each returning evidence strings) + threading + PageSpeed caching.
3. `scoring.py` + threshold + success filter.
4. Wire Stages 1–3 into a CLI run; eyeball the qualified list — **this is the milestone where lead quality is proven, before any email is written.**
5. Port contact enrichment to plain Python.
6. Single-call email writer using top-2 evidence.
7. Follow-ups, scheduling, dashboard polish.

## 11. Housekeeping (do first)

- **Rotate the Serper API key** — it is committed in plain text in README.md.
- Remove `.env` from any archives/repos; confirm `.gitignore` covers it.
- Remove `.venv/` from archives — it was 99% of the 250 MB RAR.
