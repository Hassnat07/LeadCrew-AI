"""
Stage 3 — Scoring.

Reads issues_json from each 'audited' business, applies WEIGHTS,
caps pain_score at 100, applies the success filter, writes back
the enriched issues_json as [(issue, weight, evidence), ...] sorted
by weight descending, and sets status → 'qualified' | 'disqualified'.

Also scores no_website=1 businesses still at status='harvested', and
RE-SCORES existing 'qualified' leads so filter changes / new rules apply
retroactively without requiring a full re-audit.

social_only leads (no website, but GMB listing points at Instagram/Facebook)
get a dedicated issue key with platform-specific evidence — different pitch
from a lead that has truly zero web presence.

Success filter (tiered by lead type):
  no_website / social_only : rating_count >= 20 AND rating >= 4.0
  website leads            : rating_count >= 10 AND rating >= 4.0
Below threshold → status='disqualified' (reason printed in verbose output).

Standalone usage:
    python scoring.py
"""
import json
import os
import re
import sys
from urllib.parse import urlparse

# Ensure Unicode evidence strings print safely on Windows cp1252 terminals.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from database import get_businesses, update_business_by_id, businesses_count

# ── Exclusion list ────────────────────────────────────────────────────────────

_EXCLUSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "excluded_businesses.txt")

_URLENC_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def _load_exclusions() -> tuple[set[str], set[str], set[str]]:
    """
    Parse excluded_businesses.txt.
    Returns (names_lower, domains_lower, phone_digit_strings).
    Missing file → empty sets (safe default).
    """
    names: set[str] = set()
    domains: set[str] = set()
    phones: set[str] = set()
    if not os.path.exists(_EXCLUSION_FILE):
        return names, domains, phones
    with open(_EXCLUSION_FILE, encoding="utf-8") as fh:
        for line in fh:
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            lo = entry.lower()
            if " " not in entry and "." in entry and "@" not in entry:
                domains.add(lo)
            elif re.sub(r"[\d\s+\-(). ]+", "", entry) == "":
                digits = re.sub(r"\D", "", entry)
                if digits:
                    phones.add(digits)
            else:
                names.add(lo)
    return names, domains, phones


def _is_excluded(biz: dict, names: set[str], domains: set[str], phones: set[str]) -> bool:
    if (biz.get("domain") or "").lower().strip() in domains:
        return True
    if (biz.get("name") or "").lower().strip() in names:
        return True
    phone_digits = re.sub(r"\D", "", biz.get("phone") or "")
    if phone_digits and phone_digits in phones:
        return True
    return False


# ── Social handle extraction ──────────────────────────────────────────────────

def _social_handle(social_links_json: str) -> str:
    """Return a human-readable 'Platform (@handle)' string for evidence text."""
    try:
        links: list[str] = json.loads(social_links_json or "[]")
        if not links:
            return "social media"
        url = links[0]
        path = urlparse(url).path.strip("/")
        # Drop numeric-only segments (Facebook page IDs etc.)
        segments = [s for s in path.split("/") if s and not s.isdigit()]
        handle = segments[-1] if segments else ""
        if "instagram" in url:
            return f"Instagram (@{handle})" if handle else "Instagram"
        if "facebook" in url:
            return f"Facebook ({handle})" if handle else "Facebook"
        if "tiktok" in url:
            return f"TikTok (@{handle})" if handle else "TikTok"
        if "linkedin" in url:
            return f"LinkedIn ({handle})" if handle else "LinkedIn"
        if "x.com" in url or "twitter" in url:
            return f"X/Twitter (@{handle})" if handle else "X/Twitter"
        return url
    except Exception:
        return "social media"


# ── Weights — from LEADGEN_V2_SPEC.md §4 ─────────────────────────────────────

WEIGHTS: dict[str, int] = {
    "no_website":         100,  # no web presence at all
    "social_only":        100,  # GMB points at Instagram/Facebook — no real website
    "site_down":           90,
    "no_ssl":              35,
    "mobile_score_lt_40":  30,
    "not_mobile_ready":    25,
    "no_booking_system":   22,
    "mobile_score_lt_60":  18,
    "stale_copyright":     15,
    "broken_links":        15,
    "lcp_gt_4s":           15,
    "no_analytics":        12,
    "missing_title_meta":  12,
    "outdated_tech":       12,
    "large_page_weight":   10,
    "old_site_builder":    10,
    "free_email_domain":   10,
    "no_schema":            8,
    "dead_social_links":    8,
    "missing_h1":           6,
    "no_chatbot":           5,
    "no_contact_form":      5,
}

QUALIFY_THRESHOLD = 40


# ── Core scoring logic ────────────────────────────────────────────────────────

def score_business(biz: dict) -> tuple[int, list[tuple[str, int, str]]]:
    """
    Compute pain score for one business.

    Returns (pain_score, [(issue, weight, evidence), ...]) sorted weight-desc.
    The returned list is stored as issues_json — top 2 items become the email
    hook in Stage 5.

    no_website leads with a social_links URL get the social_only issue key
    (same 100-pt weight, different evidence and pitch).
    """
    if biz.get("no_website"):
        social = biz.get("social_links")
        if social:
            platform = _social_handle(social)
            return 100, [(
                "social_only", 100,
                f"Only web presence is {platform} — no website for Google to index"
            )]
        return 100, [(
            "no_website", 100,
            "Business has no website — highest-value lead type for web design"
        )]

    try:
        raw = json.loads(biz.get("issues_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        return 0, []

    seen: set[str] = set()
    scored: list[tuple[str, int, str]] = []

    for item in raw:
        # Accept (key, evidence) from audit_runner and (key, weight, evidence)
        # from a previous scoring run — makes scoring fully idempotent.
        if len(item) == 2:
            key, evidence = item[0], item[1]
        elif len(item) == 3:
            key, _, evidence = item[0], item[1], item[2]
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        w = WEIGHTS.get(key, 0)
        if w > 0:
            scored.append((key, w, evidence))

    scored.sort(key=lambda t: -t[1])
    pain_score = min(100, sum(w for _, w, _ in scored))
    return pain_score, scored


def passes_success_filter(biz: dict) -> tuple[bool, str]:
    """
    Returns (passes, reason_if_failed).

    no_website / social_only leads : rating_count >= 20 AND rating >= 4.0
    website leads                  : rating_count >= 10 AND rating >= 4.0
    """
    try:
        rc = int(biz.get("rating_count") or 0)
        r  = float(biz.get("rating") or 0.0)
    except (TypeError, ValueError):
        return False, "no rating data"

    if biz.get("no_website"):
        if rc >= 20 and r >= 4.0:
            return True, ""
        return False, f"too small -- {rc} review{'s' if rc != 1 else ''}, {r:.1f}*"

    # website lead
    if rc >= 10 and r >= 4.0:
        return True, ""
    return False, f"too small — {rc} review{'s' if rc != 1 else ''}, {r:.1f}★"


def _compute_grade(pain_score: int) -> str:
    if pain_score >= 80:
        return "A+"
    if pain_score >= 60:
        return "A"
    return "B"


# ── Batch scoring ─────────────────────────────────────────────────────────────

def score_all(verbose: bool = True) -> dict:
    """
    Three-pass scoring run:

    1. Retroactive exclusion: re-check all 'qualified' leads against
       excluded_businesses.txt — newly matching leads are set 'excluded'.

    2. Stale-evidence reset: any lead whose issues_json contains URL-encoded
       characters (e.g. %20) is reset to 'harvested' for re-audit.

    3. Main scoring pass: score every 'audited' lead, every 'harvested'
       no_website lead, and every currently-'qualified' lead (so new filter
       rules and the social_only fix apply retroactively). Writes pain_score,
       grade, issues_json, and status back to the DB.

    Returns dict with keys: total, qualified, disqualified, excluded, stale_reset.
    """
    excl_names, excl_domains, excl_phones = _load_exclusions()

    # ── Pre-pass 1: retroactive exclusion check on already-qualified leads ───
    retro_excl = 0
    for biz in get_businesses(status="qualified"):
        if _is_excluded(biz, excl_names, excl_domains, excl_phones):
            update_business_by_id(biz["id"], status="excluded")
            retro_excl += 1
            if verbose:
                print(f"  [EXCL retro]  {(biz.get('name') or '')[:45]}")

    # ── Pre-pass 2: stale-evidence reset ─────────────────────────────────────
    stale_reset = 0
    for biz in get_businesses():
        if biz.get("status") in ("excluded", "harvested"):
            continue
        if _URLENC_RE.search(biz.get("issues_json") or ""):
            update_business_by_id(biz["id"], status="harvested")
            stale_reset += 1
            if verbose:
                print(f"  [STALE reset] {(biz.get('name') or '')[:45]}  (reset for re-audit)")

    if verbose and stale_reset:
        print(
            f"\n  {stale_reset} lead(s) reset to 'harvested' (URL-encoded evidence). "
            f"Run audit_runner.py to refresh them.\n"
        )

    # ── Main scoring pass ─────────────────────────────────────────────────────
    # Include currently-qualified leads so filter + social_only changes apply
    # retroactively without requiring a full re-audit cycle.
    audited           = get_businesses(status="audited")
    no_web            = [b for b in get_businesses(status="harvested") if b.get("no_website")]
    current_qualified = get_businesses(status="qualified")
    all_biz           = audited + no_web + current_qualified

    qualified = disqualified = excluded = 0

    for biz in all_biz:
        if _is_excluded(biz, excl_names, excl_domains, excl_phones):
            update_business_by_id(biz["id"], status="excluded")
            excluded += 1
            if verbose:
                print(f"  [EXCL]        {(biz.get('name') or '')[:45]}")
            continue

        pain_score, scored_issues = score_business(biz)
        ok, reason = passes_success_filter(biz)

        if pain_score >= QUALIFY_THRESHOLD and ok:
            grade  = _compute_grade(pain_score)
            status = "qualified"
            qualified += 1
        else:
            grade  = None
            status = "disqualified"
            disqualified += 1
            if not reason:
                reason = f"pain_score {pain_score} < {QUALIFY_THRESHOLD}"

        update_business_by_id(
            biz["id"],
            pain_score  = pain_score,
            issues_json = json.dumps(scored_issues),
            grade       = grade,
            status      = status,
        )

        if verbose:
            name = (biz.get("name") or "")[:45]
            if status == "qualified":
                print(f"  [QUAL] {pain_score:3}/100  [{grade}]  {name}")
            else:
                print(f"  [skip] {pain_score:3}/100        {name}  ({reason})")

    return {
        "total":        len(all_biz),
        "qualified":    qualified,
        "disqualified": disqualified,
        "excluded":     excluded + retro_excl,
        "stale_reset":  stale_reset,
    }


# ── Standalone CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    audited_count  = businesses_count(status="audited")
    harvested_now  = businesses_count(status="harvested")
    qualified_now  = businesses_count(status="qualified")

    if audited_count == 0 and harvested_now == 0 and qualified_now == 0:
        print("No businesses found to score. Run audit_runner.py first.")
        sys.exit(0)

    print(
        f"Scoring {audited_count} audited + "
        f"{qualified_now} currently-qualified (re-score) + "
        f"any harvested no_website leads...\n"
    )
    stats = score_all(verbose=True)

    print(
        f"\n  Total processed : {stats['total']}\n"
        f"  Qualified       : {stats['qualified']}  "
        f"(pain_score >= {QUALIFY_THRESHOLD} + success filter)\n"
        f"  Disqualified    : {stats['disqualified']}\n"
        f"  Excluded        : {stats['excluded']}  "
        f"(matched excluded_businesses.txt)\n"
        f"  Stale reset     : {stats['stale_reset']}  "
        f"(reset for re-audit — run audit_runner.py)\n"
        f"\n  Run run_pipeline.py --score-only to see the corrected leads report."
    )
