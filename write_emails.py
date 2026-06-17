"""
Stage 5 — Email draft writer.

One LLM call per qualified website lead. Drafts saved to email_drafts table
for review — nothing is sent. Leads in Rome get Italian; all others English.

Lead selection:
  - status='qualified', no_website=0 (website leads only)
  - Ordered by pain_score DESC, rating_count DESC (best leads first)
  - Skips leads already drafted unless --redo

Subject selection:
  - Scan ALL scored issues (weight-desc order already stored)
  - Skip SUBJECT_BANNED_KEYS (binary/technical with no concrete number)
  - Pick highest-weight remaining; it becomes the subject anchor
  - Non-numeric issue keys: rotate through pre-written variant phrases
    so identical subjects never repeat within the same batch
  - Numeric-evidence keys (mobile score, LCP, page weight): LLM generates
    from the concrete number -- naturally unique per lead

Subject validation:
  - Subject <= 60 chars, no banned jargon, no duplicate within the batch

Usage:
    python write_emails.py --limit 5      # draft top 5 for review
    python write_emails.py                # draft all un-drafted leads
    python write_emails.py --redo         # delete old drafts, regenerate
    python write_emails.py --model gpt-4o # override model
"""

import json
import os
import re
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from openai import OpenAI

from database import _conn, _init


# ── Subject selection rules ───────────────────────────────────────────────────

SUBJECT_BANNED_KEYS: frozenset[str] = frozenset({
    "not_mobile_ready",
    "no_schema",
    "missing_h1",
    "no_analytics",
    "no_chatbot",
    "no_contact_form",
    "outdated_tech",
    "dead_social_links",
    "missing_title_meta",
})

SUBJECT_JARGON: frozenset[str] = frozenset({
    "viewport", "audit", "schema", "lcp", "pagespeed", "seo",
    "h1", "analytics", "pixel", "markup", "mobile-friendly",
    "mobile friendly", "structured data", "sitemap", "canonical",
    "meta", "tag", "404",
})


def _extract_number(evidence: str) -> str:
    m = re.search(r"\d+\.?\d*", evidence)
    return m.group(0) if m else ""


def _pick_subject_issue(all_issues: list[tuple]) -> tuple | None:
    # Priority 1: highest-weight non-banned issue with a concrete number in evidence
    # (mobile score, LCP seconds, page MB, copyright year, site-down codes, etc.)
    for issue in all_issues:
        if issue[0] in SUBJECT_BANNED_KEYS:
            continue
        evidence = issue[2] if len(issue) > 2 else ""
        if _extract_number(evidence):
            return issue
    # Priority 2: highest-weight non-banned issue (no translatable numeric evidence)
    for issue in all_issues:
        if issue[0] not in SUBJECT_BANNED_KEYS:
            return issue
    return all_issues[0] if all_issues else None


# ── Subject variant pools (non-numeric issue keys) ────────────────────────────
# Numeric keys (mobile_score_lt_40, lcp_gt_4s, large_page_weight, stale_copyright)
# produce naturally unique subjects because the evidence number differs per lead.
# Non-numeric keys would produce identical subjects for every lead in a niche --
# these pools prevent that by rotating through pre-written phrasings.

_IT_SUBJECT_VARIANTS: dict[str, list[str]] = {
    "no_booking_system": [
        "Nessun sistema di prenotazione online",
        "I clienti non possono prenotare dal sito",
        "Prenotazioni: solo per telefono?",
        "Il sito non accetta prenotazioni online",
    ],
    "no_website": [
        "La vostra attività non ha sito web",
        "Non siete trovabili su Google",
        "Nessun sito web per la vostra attività",
    ],
    "social_only": [
        "Solo Instagram — nessun sito web",
        "La vostra presenza online è solo social",
        "Nessun sito: solo profilo Instagram",
    ],
    "site_down": [
        "Il vostro sito non è raggiungibile",
        "Il sito risulta offline",
        "Il sito non si apre",
    ],
    "no_ssl": [
        "Il sito non è sicuro (manca HTTPS)",
        "Nessun certificato HTTPS sul sito",
        "Il sito viene segnalato come non sicuro",
    ],
    "broken_links": [
        "Link rotti trovati sul vostro sito",
        "Pagine non trovate sul vostro sito",
        "Il sito ha link che non funzionano",
    ],
    "free_email_domain": [
        "L'email aziendale è su Gmail",
        "Email professionale mancante sul sito",
        "Il sito usa un indirizzo email gratuito",
    ],
    "old_site_builder": [
        "Il sito usa un costruttore obsoleto",
        "Il sito è costruito su tecnologia datata",
    ],
}

_EN_SUBJECT_VARIANTS: dict[str, list[str]] = {
    "no_booking_system": [
        "No online booking on your site",
        "Clients can't book through your website",
        "Bookings: phone calls only?",
        "Your site doesn't take reservations",
    ],
    "no_website": [
        "Your business has no website",
        "You're not findable on Google",
        "No website found for your business",
    ],
    "social_only": [
        "Instagram only — no real website",
        "Your only online presence is social media",
        "No website: only an Instagram profile",
    ],
    "site_down": [
        "Your website is currently unreachable",
        "Your site appears to be offline",
        "Your site won't load",
    ],
    "no_ssl": [
        "Your site is flagged as not secure",
        "No HTTPS on your site",
        "Browsers warn visitors your site isn't safe",
    ],
    "broken_links": [
        "Broken links found on your website",
        "Some pages on your site return errors",
        "Your site has links that don't work",
    ],
    "free_email_domain": [
        "Your business email is on Gmail",
        "No professional email on your site",
        "Your site uses a free email address",
    ],
    "old_site_builder": [
        "Your website builder is obsolete",
        "Your site is built on outdated technology",
    ],
}


# ── Body cost-sentence variant pools ─────────────────────────────────────────
# 3 phrasings per key; rotate by (key_use_count % 3) so the "business cost"
# paragraph sounds fresh when the same issue anchors multiple emails in a batch.

_IT_COST_VARIANTS: dict[str, list[str]] = {
    "no_booking_system": [
        "Ogni cliente che cerca di prenotare online e non ci riesce rischia di andare dalla concorrenza.",
        "Chi visita il sito fuori orario non può fare nulla — e spesso non richiama.",
        "Senza prenotazione online, ogni cliente serale o del weekend è una prenotazione persa.",
    ],
    "mobile_score_lt_40": [
        "Un sito lento su mobile perde oltre la metà dei visitatori prima ancora che la pagina si apra.",
        "Su telefono, se il sito non si apre subito, il cliente chiude e cerca un altro.",
        "La maggior parte delle ricerche locali avviene da telefono — un sito lento li manda via.",
    ],
    "lcp_gt_4s": [
        "Tre secondi di attesa su mobile e il 53% degli utenti abbandona la pagina.",
        "Su telefono, se il sito non si apre subito, il cliente chiude e cerca un altro.",
        "Un sito lento su mobile perde oltre la metà dei visitatori prima che la pagina si apra.",
    ],
    "site_down": [
        "Un sito offline è come un negozio con le luci spente — i clienti passano oltre.",
        "Ogni ora offline è visibilità persa e potenziali clienti che trovano un concorrente.",
        "Quando il sito non si apre, Google lo nota e abbassa il ranking.",
    ],
    "no_ssl": [
        "Chrome mostra 'Non sicuro' accanto al vostro sito — molti utenti chiudono immediatamente.",
        "Senza HTTPS i clienti vedono un avviso di pericolo prima ancora di leggere il sito.",
        "Un sito senza certificato perde fiducia e subisce penalizzazioni nel ranking Google.",
    ],
    "no_website": [
        "Chi cerca online non vi trova — e va dalla concorrenza che ha un sito.",
        "Senza sito, Google non può indicizzarvi: siete invisibili a migliaia di ricerche al mese.",
        "Il passaparola porta fin qui, ma chi non vi conosce ancora non vi trova da nessuna parte.",
    ],
    "social_only": [
        "Instagram non viene indicizzato da Google — chi cerca i vostri servizi non vi trova.",
        "Un profilo social non basta: Google non mostra le pagine Instagram nei risultati locali.",
        "Chi cerca i vostri servizi su Google non trova il vostro profilo Instagram.",
    ],
    "large_page_weight": [
        "Una pagina pesante si carica lentamente su ogni connessione mobile — i clienti si stancano ad aspettare.",
        "Con una connessione 4G normale, una pagina da molti MB impiega secondi a caricarsi — i visitatori se ne vanno.",
        "Un sito pesante consuma i dati mobili dei visitatori e li spinge a chiudere prima ancora di leggere.",
    ],
    "stale_copyright": [
        "Una data di copyright vecchia segnala che il sito non viene aggiornato — e i clienti lo notano.",
        "Il copyright fermo a vecchi anni trasmette abbandono — i visitatori si chiedono se l'attività è ancora aperta.",
        "Un anno di copyright datato è il primo segnale di un sito non curato.",
    ],
}

_EN_COST_VARIANTS: dict[str, list[str]] = {
    "no_booking_system": [
        "Every potential customer who can't book online is one more appointment your competitor gets.",
        "Without online booking, anyone who finds you after hours has no way to act on it.",
        "People expect to book at 11pm — without it, they move on to whoever makes it easy.",
    ],
    "mobile_score_lt_40": [
        "Over half of mobile visitors leave before a slow page finishes loading — that's lost business.",
        "Most local searches happen on a phone — a site that won't load sends them straight to a competitor.",
        "A slow mobile site also means lower Google rankings and fewer people finding you at all.",
    ],
    "lcp_gt_4s": [
        "53% of mobile users abandon a page if it takes more than 3 seconds to open.",
        "Most local searches happen on a phone — a site that won't load sends them straight to a competitor.",
        "Over half of mobile visitors leave before a slow page finishes loading.",
    ],
    "site_down": [
        "An offline site is a closed door — potential customers move on to whoever they can actually reach.",
        "Every hour the site is down is visibility lost and competitors gaining ground.",
        "When a site won't load, Google notices and lowers its ranking.",
    ],
    "no_ssl": [
        "Chrome shows 'Not secure' next to your site — many visitors close it immediately.",
        "Without HTTPS, customers see a danger warning before they even read your page.",
        "An insecure site loses visitor trust and gets penalized in Google rankings.",
    ],
    "no_website": [
        "Anyone searching online can't find you — and they go to a competitor who has a site.",
        "Without a site, Google can't index you: you're invisible to thousands of monthly searches.",
        "Word of mouth brings people here, but anyone who hasn't heard of you yet finds nothing.",
    ],
    "social_only": [
        "Instagram isn't indexed by Google — anyone searching for your services won't find you.",
        "A social profile isn't enough: Google doesn't show Instagram pages in local search results.",
        "People searching on Google for your services won't see your Instagram profile.",
    ],
    "large_page_weight": [
        "A heavy page loads slowly on any mobile connection — visitors give up before it finishes.",
        "On a normal 4G connection, a multi-MB page takes seconds to load — visitors leave.",
        "A bloated site eats mobile data and pushes visitors to close it before reading anything.",
    ],
    "stale_copyright": [
        "An old copyright year signals the site hasn't been updated — customers notice.",
        "A stale copyright date suggests neglect — visitors wonder if the business is still active.",
        "An outdated year is the first signal of a site no one maintains.",
    ],
}


def _get_subject_variant(key: str, language: str, idx: int) -> str | None:
    """Pre-written subject for non-numeric keys; None for numeric keys (LLM generates)."""
    pool = _IT_SUBJECT_VARIANTS if language == "it" else _EN_SUBJECT_VARIANTS
    variants = pool.get(key)
    return variants[idx % len(variants)] if variants else None


def _get_cost_variant(key: str, language: str, idx: int) -> str | None:
    """Cost-sentence variant for the body; None if key not in pool (LLM writes freely)."""
    pool = _IT_COST_VARIANTS if language == "it" else _EN_COST_VARIANTS
    variants = pool.get(key)
    return variants[idx % len(variants)] if variants else None


# ── Table setup ───────────────────────────────────────────────────────────────

def _init_drafts() -> None:
    _init()
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS email_drafts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id    INTEGER NOT NULL,
                to_email       TEXT,
                subject        TEXT NOT NULL,
                body           TEXT NOT NULL,
                language       TEXT NOT NULL DEFAULT 'en',
                model          TEXT,
                generated_at   TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'draft'
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_draft_biz "
            "ON email_drafts(business_id)"
        )


def _already_drafted(business_id: int) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM email_drafts WHERE business_id = ? AND status = 'draft' LIMIT 1",
            (business_id,),
        ).fetchone()
        return row is not None


def _delete_drafts(business_id: int) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM email_drafts WHERE business_id = ?",
            (business_id,),
        )


def _save_draft(
    business_id: int,
    to_email: str | None,
    subject: str,
    body: str,
    language: str,
    model: str,
) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO email_drafts
               (business_id, to_email, subject, body, language, model, generated_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')""",
            (
                business_id, to_email, subject, body, language, model,
                datetime.now().isoformat(),
            ),
        )
        return cur.lastrowid


# ── Language ──────────────────────────────────────────────────────────────────

_ITALIAN_CITIES: frozenset[str] = frozenset({"rome", "roma"})


def _detect_language(city: str) -> str:
    return "it" if city.lower().strip() in _ITALIAN_CITIES else "en"


# ── Validation ────────────────────────────────────────────────────────────────

def _validate_draft(
    subject: str,
    body: str,
    all_issues: list[tuple],
    seen_subjects: set[str] | None = None,
) -> tuple[bool, list[str]]:
    """
    Returns (passes, [failure reasons]).

    Checks:
      1. subject <= 60 chars
      2. subject contains no banned jargon words
      3. body < 160 words
      4. body contains at least one exact number drawn from top-2 evidence
      5. subject not already used earlier in this batch (duplicate check)
    """
    failures: list[str] = []

    if len(subject) > 60:
        failures.append(f"subject {len(subject)} chars (max 60)")

    subj_lo = subject.lower()
    for word in SUBJECT_JARGON:
        if word in subj_lo:
            failures.append(f"jargon '{word}' in subject")
            break

    wc = len(body.split())
    if wc >= 160:
        failures.append(f"body {wc} words (max 159)")

    ev_numbers = set()
    for issue in all_issues[:2]:
        evidence = issue[2] if len(issue) > 2 else ""
        for n in re.findall(r"\d+\.?\d*", evidence):
            if len(n) >= 2:
                ev_numbers.add(n)
    body_numbers = set(re.findall(r"\d+\.?\d*", body))
    if ev_numbers and not (ev_numbers & body_numbers):
        failures.append(f"body missing number from evidence (expected one of: {sorted(ev_numbers)[:4]})")

    if seen_subjects is not None and subject.lower() in seen_subjects:
        failures.append("duplicate subject in this batch")

    return len(failures) == 0, failures


# ── Prompt ────────────────────────────────────────────────────────────────────

def _build_prompt(
    biz: dict,
    subject_issue: tuple,
    body_issues: list[tuple],
    language: str,
    subject_override: str | None = None,
    cost_hint: str | None = None,
    avoid_subjects: list[str] | None = None,
    opening_frame: int = 0,
) -> str:
    name     = biz.get("name") or ""
    city     = biz.get("city") or ""
    category = (biz.get("category") or biz.get("niche") or "").strip()
    rating   = float(biz.get("rating") or 0)
    rc       = int(biz.get("rating_count") or 0)

    subj_key = subject_issue[0]
    subj_ev  = subject_issue[2] if len(subject_issue) > 2 else ""
    subj_num = _extract_number(subj_ev)

    body_lines = "\n".join(
        f"{i + 1}. [{t[0]}] {t[2]}"
        for i, t in enumerate(body_issues)
    )

    if language == "it":
        lang_note = (
            "Write entirely in natural, professional Italian. "
            "Findings below are in English — adapt them naturally into Italian. "
            "The Italian must be flawless and native-level; pay special attention "
            "to grammatical gender and number agreement (e.g. 'le recensioni', "
            "'degli studi', 'i clienti')."
        )
        sign_off     = "Hassnat\nHaxantech"
        sender_intro = (
            '"Sono Hassnat di Haxantech — aiutiamo attività locali a '
            'trasformare il sito in clienti."'
        )
        value_offer  = (
            '"Se vi interessa, vi mando una breve analisi gratuita del sito '
            'con i 3 problemi principali — senza impegno."'
        )
        tiny_yes_cta = '"Vi va se ve la mando?" (a yes/no question — nothing more)'
        subj_bad     = (
            '"Il tuo sito non è mobile-friendly: viewport audit failed"  '
            "<- too long, uses jargon, no useful number"
        )
        subj_good_examples = (
            '"Nessun sistema di prenotazione online"  (37 chars)\n'
            '"Il sito si apre in 8 secondi sul telefono"  (42 chars)\n'
            '"Il tuo sito pesa 16 MB: troppo lento"  (38 chars)\n'
            '"Punteggio mobile: 29/100"  (25 chars)'
        )
    else:
        lang_note = "Write in English."
        sign_off     = "Hassnat\nHaxantech"
        sender_intro = (
            '"I\'m Hassnat from Haxantech — we help local businesses '
            'turn their website into paying customers."'
        )
        value_offer  = (
            '"If you\'re interested, I can send you a quick free analysis of your site '
            'showing the 3 main issues — no strings attached."'
        )
        tiny_yes_cta = '"Want me to send it over?" (a yes/no question — nothing more)'
        subj_bad     = (
            '"Your site failed the viewport audit"  '
            "<- jargon, no number"
        )
        subj_good_examples = (
            '"No online booking on your site"  (31 chars)\n'
            '"Your site loads in 8 seconds on mobile"  (38 chars)\n'
            '"Your mobile score: 29/100"  (26 chars)\n'
            '"Your page is 16 MB — too slow"  (30 chars)'
        )

    # Subject section: verbatim if pre-selected; full guidance otherwise
    if subject_override:
        subject_section = (
            f"━━━ SUBJECT ━━━\n"
            f"Use this exact subject line — copy it verbatim, do not change a single character:\n"
            f'"{subject_override}"\n'
        )
    else:
        num_hint = (
            f"It contains the number {subj_num} — include it in the subject."
            if subj_num else
            "There is no numeric stat — write a plain-words fact instead."
        )
        translation_hints = ""
        body_ev_str = " ".join(t[2] for t in body_issues).lower()
        if "lcp" in subj_ev.lower() or "lcp" in body_ev_str:
            if language == "it":
                translation_hints += (
                    '\nNOTE: "LCP is X.Xs" means the page takes X seconds to open on mobile'
                    ' — phrase it as "si apre in X secondi sul telefono".'
                )
            else:
                translation_hints += (
                    '\nNOTE: "LCP is X.Xs" means the page takes X seconds to open on mobile'
                    ' — phrase it as "loads in X seconds on mobile".'
                )
        if subj_key == "large_page_weight" or any(t[0] == "large_page_weight" for t in body_issues):
            if language == "it":
                translation_hints += (
                    '\nNOTE: the page-weight finding (e.g. "24.7 MB") means the page is that'
                    ' heavy to download — phrase it as "il sito pesa X MB" (round to whole number).'
                )
            else:
                translation_hints += (
                    '\nNOTE: the page-weight finding (e.g. "24.7 MB") means the page is that'
                    ' large — phrase it as "your page weighs X MB" (round to whole number).'
                )

        subject_section = (
            f"━━━ SUBJECT (anchor issue: [{subj_key}]) ━━━\n"
            f'Finding to anchor the subject: "{subj_ev}"\n'
            f"{num_hint}{translation_hints}\n"
            f"\n"
            f"Subject rules — all are hard requirements:\n"
            f"• Maximum 60 characters (count every character including spaces)\n"
            f"• Plain language a non-technical shop owner immediately understands\n"
            f"• Exactly one concrete number or concrete fact — not a vague claim\n"
            f"• NEVER use these words: viewport, audit, schema, LCP, PageSpeed, SEO, H1, analytics, pixel, markup, mobile-friendly, structured data, sitemap, meta, 404\n"
            f"• Rating / review count goes in the body — NOT in the subject\n"
            f"• Do NOT start with 'Il tuo sito' or 'Your website' — vary the opening\n"
            f"\n"
            f"BAD subject: {subj_bad}\n"
            f"GOOD subject examples (one of these styles):\n"
            f"{subj_good_examples}\n"
        )
        # Overflow mode: pool exhausted — LLM must produce a fresh variant
        if avoid_subjects:
            avoid_lines = "\n".join(f'  "{s}"' for s in avoid_subjects)
            subject_section += (
                f"\nCRITICAL — these subjects are already used in this batch; "
                f"do NOT repeat any of them:\n{avoid_lines}\n"
                f"Write a DIFFERENT subject that conveys the same problem in new words.\n"
            )

    # Cost paragraph: anchored phrasing if in pool, free write otherwise
    if cost_hint:
        cost_instruction = (
            f"Use this cost sentence as your starting point "
            f"(adapt names/details naturally, keep the core message): "
            f'"{cost_hint}"'
        )
    else:
        cost_instruction = (
            "End this paragraph with the business cost in plain words "
            "(lost customers, missed bookings, lower ranking, etc.)"
        )

    # Opening frame rotation — 3 frames, cycled across the batch
    frame_type = opening_frame % 3
    if language == "it":
        if frame_type == 0:
            opening_frame_instruction = (
                f"COMPLIMENTO → CONTRASTO: apri con un genuino complimento "
                f"sulle {rc} recensioni o le {rating} stelle di {name}, "
                f"poi contrasta con il problema principale."
            )
        elif frame_type == 1:
            opening_frame_instruction = (
                f"DOMANDA: inizia direttamente con una domanda che mette in contrasto "
                f"il risultato impressionante ({rc} recensioni / {rating} stelle) con il "
                f"problema specifico — es. '{rc} recensioni e {rating} stelle — perché il "
                f"sito si apre in 8 secondi?' NON iniziare con 'Complimenti'."
            )
        else:
            opening_frame_instruction = (
                f"OSSERVAZIONE: inizia con 'Stavo guardando [i/le {category} più "
                f"recensiti/e di {city}]...' e cita una cosa vera e specifica su "
                f"{name} (es. il numero di recensioni, la loro reputazione). "
                f"NON iniziare con 'Complimenti'."
            )
    else:
        if frame_type == 0:
            opening_frame_instruction = (
                f"COMPLIMENT → CONTRAST: open with a genuine compliment on "
                f"{name}'s {rc} reviews or {rating} stars, then pivot to the core problem."
            )
        elif frame_type == 1:
            opening_frame_instruction = (
                f"QUESTION opener: start directly with a question pairing their impressive "
                f"{rc} reviews / {rating} stars with the specific problem — e.g., "
                f"'2,100 reviews and 5 stars — why does the site take 8 seconds to open?' "
                f"Do NOT start with a compliment."
            )
        else:
            opening_frame_instruction = (
                f"OBSERVATION opener: start with 'I was looking at the top-reviewed "
                f"{category} in {city}...' and name one specific true fact about "
                f"{name} (their high review count, prominent reputation, etc.). "
                f"Do NOT start with a compliment."
            )

    return f"""You are writing a personalized cold email for Hassnat at Haxantech, a web agency building fast modern websites for local businesses. {lang_note}

Return a JSON object with exactly two keys: "subject" and "body".

━━━ TARGET BUSINESS ━━━
Name:     {name}
City:     {city}
Type:     {category}
Google:   {rating} stars, {rc} reviews

{subject_section}
━━━ BODY ━━━
Top 2 findings to use (exact figures — use them verbatim):
{body_lines}

Body rules — write exactly 3–4 short paragraphs separated by blank lines. Plain text only — no bullet points, no HTML in the output.

PARAGRAPH 1 — HOOK:
{opening_frame_instruction}

PARAGRAPH 2 — PROBLEM + COST:
State the specific problem with the EXACT number/fact from the finding — phrased as a business fact, not a tool output. {cost_instruction}

PARAGRAPH 3 — OFFER + INTRO + CTA:
Value offer (give something before asking): {value_offer}
Sender intro — place it HERE, after the offer, before the CTA: {sender_intro}
  - This is the ONLY place the sender/company name appears
  - No "years of experience", no service list, no company history
Tiny-yes CTA — one effortless yes/no question: {tiny_yes_cta}
  - NOT "book a call" — too big a step
  - NOT "schedule a meeting" — too demanding
  - NOT price talk of any kind

SIGN-OFF — after a blank line, on its own lines, exactly:
{sign_off}
(each name on a separate line — NEVER write them inline after a sentence)

Hard rules:
• Under 160 words total
• DO NOT open with who we are — the business's facts come first
• No jargon anywhere in the body: no LCP, no viewport, no schema, no PageSpeed — translate every finding into a plain business fact"""


# ── LLM call ─────────────────────────────────────────────────────────────────

def _generate_email(
    client: OpenAI,
    biz: dict,
    all_issues: list[tuple],
    language: str,
    model: str,
    variant_idx: int = 0,
    used_subjects_for_key: list[str] | None = None,
    opening_frame: int = 0,
) -> tuple[str, str]:
    """Return (subject, body). Raises on failure."""
    subject_issue = _pick_subject_issue(all_issues)
    if not subject_issue:
        raise ValueError("No issues available for this lead")

    subj_key  = subject_issue[0]
    cost_hint = _get_cost_variant(subj_key, language, variant_idx)

    # Pool range: enforce pre-written variant (no LLM choice, no jargon risk).
    # Pool exhausted: LLM generates a fresh variant constrained by avoid list.
    pool      = _IT_SUBJECT_VARIANTS if language == "it" else _EN_SUBJECT_VARIANTS
    pool_size = len(pool.get(subj_key, []))

    if variant_idx < pool_size:
        subject_override = _get_subject_variant(subj_key, language, variant_idx)
        avoid_subjects   = None
    else:
        subject_override = None
        avoid_subjects   = list(used_subjects_for_key or [])

    body_issues = all_issues[:2]
    prompt      = _build_prompt(
        biz, subject_issue, body_issues, language,
        subject_override=subject_override,
        cost_hint=cost_hint,
        avoid_subjects=avoid_subjects,
        opening_frame=opening_frame,
    )

    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.7,
        max_tokens=700,
    )
    raw  = resp.choices[0].message.content or ""
    data = json.loads(raw)

    subject = (data.get("subject") or "").strip()
    body    = (data.get("body")    or "").strip()
    if not subject or not body:
        raise ValueError(f"Incomplete response: {raw[:200]}")

    # For pre-written subjects, enforce the exact string regardless of LLM output --
    # guarantees no jargon creep, correct char count, and no within-batch duplicates.
    if subject_override:
        subject = subject_override

    return subject, body


# ── Display ───────────────────────────────────────────────────────────────────

_DIV = "─" * 68


def _print_draft(
    rank: int,
    biz: dict,
    subject: str,
    body: str,
    language: str,
    draft_id: int,
    to_email: str | None,
    passes: bool,
    failures: list[str],
) -> None:
    verdict = "PASS" if passes else f"FAIL — {'; '.join(failures)}"

    name  = biz.get("name") or ""
    city  = biz.get("city") or ""
    grade = biz.get("grade") or ""
    score = biz.get("pain_score") or 0
    rc    = biz.get("rating_count") or 0
    wc    = len(body.split())

    print(f"\n{'='*68}")
    print(f"  #{rank}  [{grade}]  {name}  |  {city}  ({score}/100, {rc} reviews)")
    print(f"  Language : {'Italian' if language == 'it' else 'English'}")
    print(f"  To       : {to_email or '(pending — run enrich_contacts.py)'}")
    print(f"  Draft ID : {draft_id}")
    print(f"  Valid    : {verdict}")
    print(f"  Subject  : {subject}  ({len(subject)} chars)")
    print(f"  Words    : {wc}")
    print()
    print(f"  {_DIV}")
    for line in body.splitlines():
        print(f"  {line}")
    print(f"  {_DIV}")


# ── Main ──────────────────────────────────────────────────────────────────────

def write_emails(
    limit: int | None = None,
    redo: bool = False,
    model: str = "gpt-4o",
    api_key: str = "",
) -> None:
    _init_drafts()
    client = OpenAI(api_key=api_key)

    with _conn() as c:
        rows = c.execute("""
            SELECT id, name, city, niche, category, rating, rating_count,
                   no_website, grade, pain_score, issues_json, email
            FROM seen_businesses
            WHERE status = 'qualified'
              AND (
                no_website = 0
                OR (no_website = 1 AND email IS NOT NULL AND email != '')
              )
            ORDER BY pain_score DESC, rating_count DESC
        """).fetchall()

    targets = [dict(r) for r in rows]

    if not redo:
        targets = [t for t in targets if not _already_drafted(t["id"])]

    if limit is not None:
        targets = targets[:limit]

    if not targets:
        print("No qualified leads with email to draft.")
        print("  - Already drafted? Use --redo to regenerate.")
        print("  - No qualified leads? Run scoring.py first.")
        return

    print(f"\n  Stage 5 — Email Drafts")
    print(f"  Model   : {model}")
    print(f"  Drafting: {len(targets)} lead(s)  {'(redo — replacing existing drafts)' if redo else ''}\n")

    drafted = passed = failed_v = errors = 0

    # Batch-level deduplication tracking
    key_use_count: dict[str, int]       = {}  # times each key has anchored a subject
    key_used_subjects: dict[str, list[str]] = {}  # original-case subjects per key (LLM avoidance)
    batch_used_subjects: set[str]       = set()   # lowercase subjects committed this batch

    for rank, biz in enumerate(targets, 1):
        try:
            raw_issues = json.loads(biz.get("issues_json") or "[]")
        except Exception:
            raw_issues = []

        all_issues: list[tuple] = []
        for item in raw_issues:
            if len(item) == 3:
                all_issues.append((item[0], item[1], item[2]))
            elif len(item) == 2:
                all_issues.append((item[0], 0, item[1]))

        language = _detect_language(biz.get("city") or "")
        to_email = biz.get("email") or None

        # Compute rotation index; snapshot the avoidance list before incrementing
        subj_issue   = _pick_subject_issue(all_issues)
        subj_key     = subj_issue[0] if subj_issue else ""
        variant_idx  = key_use_count.get(subj_key, 0)
        used_for_key = list(key_used_subjects.get(subj_key, []))
        key_use_count[subj_key] = variant_idx + 1

        opening_frame = rank - 1  # cycles 0→1→2→0→... across the batch

        try:
            subject, body = _generate_email(
                client, biz, all_issues, language, model,
                variant_idx=variant_idx,
                used_subjects_for_key=used_for_key,
                opening_frame=opening_frame,
            )
        except Exception as exc:
            print(f"\n  #{rank}  ERROR — {biz.get('name')}: {exc}")
            errors += 1
            continue

        if redo:
            _delete_drafts(biz["id"])

        draft_id = _save_draft(biz["id"], to_email, subject, body, language, model)
        drafted += 1

        # Record original-case subject for the LLM avoidance list of later leads
        key_used_subjects.setdefault(subj_key, []).append(subject)

        # Validate against batch history before recording the lowercase version
        passes_v, failures_v = _validate_draft(
            subject, body, all_issues, seen_subjects=batch_used_subjects
        )
        batch_used_subjects.add(subject.lower())

        if passes_v:
            passed += 1
        else:
            failed_v += 1

        _print_draft(rank, biz, subject, body, language, draft_id, to_email,
                     passes=passes_v, failures=failures_v)

    print(f"\n{'='*68}")
    print(f"  Drafted  : {drafted}/{len(targets)}")
    print(f"  Passed   : {passed}   Failed validation: {failed_v}   Errors: {errors}")
    print(f"  Saved to : email_drafts table (status='draft')")
    if drafted > 0:
        print()
        print("  Next steps:")
        print("    1. Review drafts above")
        if failed_v:
            print(f"    2. Re-run with --redo to fix the {failed_v} validation failure(s)")
        print("    3. Run: python enrich_contacts.py  (populate email addresses)")
        print("    4. Stage 6: send approved drafts")
    print(f"{'='*68}\n")


# ── Follow-up draft generation ────────────────────────────────────────────────

def _followup_prompt(original_subject: str, biz_name: str, language: str) -> str:
    if language == "it":
        return (
            f'You are writing a very short follow-up cold email in flawless, native-level Italian. '
            f'Return JSON with exactly two keys: "subject" and "body".\n\n'
            f'Original email subject: "{original_subject}"\n'
            f'Business: {biz_name}\n\n'
            f'Rules:\n'
            f'• Subject: prepend "Re: " to the original subject (verbatim)\n'
            f'• Body: under 60 words, 2 short paragraphs\n'
            f'• Paragraph 1: one sentence acknowledging they may have missed the last email '
            f'— reference the core problem from the subject in plain Italian, no jargon\n'
            f'• Paragraph 2: repeat the tiny-yes CTA: "Vi va se ve la mando?" — nothing else\n'
            f'• Sign-off on its own lines:\nHassnat\nHaxantech\n'
            f'• Plain text only — no bullet points, no HTML'
        )
    return (
        f'You are writing a very short follow-up cold email in English. '
        f'Return JSON with exactly two keys: "subject" and "body".\n\n'
        f'Original email subject: "{original_subject}"\n'
        f'Business: {biz_name}\n\n'
        f'Rules:\n'
        f'• Subject: prepend "Re: " to the original subject (verbatim)\n'
        f'• Body: under 60 words, 2 short paragraphs\n'
        f'• Paragraph 1: one sentence acknowledging they may have missed the last email '
        f'— reference the core problem from the subject in plain words, no jargon\n'
        f'• Paragraph 2: repeat the tiny-yes CTA: "Want me to send it over?" — nothing else\n'
        f'• Sign-off on its own lines:\nHassnat\nHaxantech\n'
        f'• Plain text only — no bullet points, no HTML'
    )


def write_followup_drafts(api_key: str = "", model: str = "gpt-4o") -> int:
    """
    Generate follow-up drafts for all due followups that don't yet have one.
    Returns count of drafts created.
    """
    _init_drafts()
    client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY", ""))

    from database import get_due_followups, update_followup

    due = get_due_followups()
    if not due:
        print("  No follow-ups due.")
        return 0

    print(f"\n  Follow-up drafts — {len(due)} due")
    created = 0

    for fu in due:
        if fu.get("followup_draft_id"):
            print(f"  [skip] {fu['name']} — draft already exists (id {fu['followup_draft_id']})")
            continue

        language = fu.get("draft_language") or "en"
        biz_name = fu.get("name") or ""
        original_subject = fu.get("original_subject") or ""
        to_email = fu.get("email") or None

        prompt = _followup_prompt(original_subject, biz_name, language)
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.6,
                max_tokens=300,
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            subject = (data.get("subject") or "").strip()
            body    = (data.get("body")    or "").strip()
            if not subject or not body:
                raise ValueError(f"Empty response for {biz_name}")
        except Exception as exc:
            print(f"  [error] {biz_name}: {exc}")
            continue

        draft_id = _save_draft(fu["business_id"], to_email, subject, body, language, model)
        update_followup(fu["followup_id"], followup_draft_id=draft_id, status="drafted")
        print(f"  [ok]   {biz_name}  →  draft id {draft_id}  |  {subject[:50]}")
        created += 1

    print(f"\n  Created {created} follow-up draft(s) — review in dashboard before sending.")
    return created


# ── CLI ───────────────────────────────────────────────────────────────────────

def _arg(args: list[str], flag: str, default=None):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return default


if __name__ == "__main__":
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set.")
        sys.exit(1)

    args           = sys.argv[1:]
    redo_flag      = "--redo" in args
    followups_flag = "--followups" in args
    limit_arg      = int(_arg(args, "--limit")) if "--limit" in args else None
    model_arg      = _arg(args, "--model") or "gpt-4o"

    if followups_flag:
        write_followup_drafts(api_key=api_key, model=model_arg)
    else:
        write_emails(limit=limit_arg, redo=redo_flag, model=model_arg, api_key=api_key)
