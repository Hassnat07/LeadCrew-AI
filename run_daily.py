"""
Daily automation — one command for the morning pipeline.

Picks the next (niche, city) from niches.json, then runs:
  Stage 1  harvest
  Stage 2  audit
  Stage 3  score
  Stage 4  enrich contacts
  Stage 5  write email drafts (only for newly qualified leads with emails)
  ─────────────────────────────────────────────────────
  Follow-ups  generate drafts for 3-day non-repliers
  WhatsApp    regenerate wa_leads.csv

Ends with a summary and a reminder to open the dashboard.
NO sending — that happens only from the dashboard.

Usage:
    python run_daily.py
    python run_daily.py --niche "dentists" --city "Rome, Italy"   # override rotation
    python run_daily.py --no-harvest                              # skip Stage 1
    python run_daily.py --followups-only                          # only follow-ups
"""

import json
import os
import sys
from datetime import datetime, date
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_DIR   = Path(__file__).parent
_STATE = _DIR / ".rotation_state.json"
_NICHES_FILE = _DIR / "niches.json"
_WA_CSV = _DIR / "wa_leads.csv"


# ── Rotation ──────────────────────────────────────────────────────────────────

def _load_niches() -> list[dict]:
    with open(_NICHES_FILE, encoding="utf-8") as f:
        return json.load(f)


def _load_state() -> dict:
    if _STATE.exists():
        with open(_STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"last_idx": -1, "last_run": None}


def _save_state(state: dict) -> None:
    with open(_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _next_rotation(override_niche: str = "", override_city: str = "") -> tuple[str, str]:
    if override_niche and override_city:
        return override_niche, override_city
    niches = _load_niches()
    state  = _load_state()
    idx    = (state["last_idx"] + 1) % len(niches)
    entry  = niches[idx]
    _save_state({"last_idx": idx, "last_run": date.today().isoformat()})
    return entry["niche"], entry["city"]


# ── Section header ─────────────────────────────────────────────────────────────

def _section(title: str) -> None:
    print(f"\n{'─' * 70}")
    print(f"  {title}")
    print(f"{'─' * 70}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args           = sys.argv[1:]
    no_harvest     = "--no-harvest"     in args
    followups_only = "--followups-only" in args

    def _arg(flag: str) -> str | None:
        try:
            return args[args.index(flag) + 1]
        except (ValueError, IndexError):
            return None

    override_niche = _arg("--niche") or ""
    override_city  = _arg("--city")  or ""

    serper_key  = os.getenv("SERPER_API_KEY", "")
    openai_key  = os.getenv("OPENAI_API_KEY", "")
    pagespeed_key = os.getenv("PAGESPEED_API_KEY", "")

    print(f"\n{'=' * 70}")
    print(f"  LeadCrew AI — Daily Run  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 70}")

    if followups_only:
        _section("Follow-ups only")
        if openai_key:
            from write_emails import write_followup_drafts
            write_followup_drafts(api_key=openai_key)
        else:
            print("  OPENAI_API_KEY not set — skipping follow-up draft generation")
        _finish_summary()
        return

    niche, city = _next_rotation(override_niche, override_city)
    print(f"  Niche : {niche}")
    print(f"  City  : {city}")

    harvest_stats = {"added": 0}
    audit_stats   = {"audited": 0, "unreachable": 0, "errors": 0}
    score_stats   = {"total": 0, "qualified": 0, "disqualified": 0}
    enrich_counts = {"found": 0, "not_found": 0}
    draft_count   = 0

    # ── Stage 1: Harvest ──────────────────────────────────────────────────────
    if not no_harvest:
        if not serper_key:
            print("  WARNING: SERPER_API_KEY not set — skipping harvest")
        else:
            _section(f"Stage 1 — Harvest  |  {niche} in {city}")
            from harvester import harvest
            added = harvest(niche, city, serper_key)
            harvest_stats["added"] = len(added)
            print(f"\n  → {len(added)} new businesses")
    else:
        print("  [--no-harvest] Skipping Stage 1")

    # ── Stage 2: Audit ────────────────────────────────────────────────────────
    _section("Stage 2 — Audit")
    from audit_runner import run_audit
    audit_stats = run_audit()
    print(
        f"\n  → {audit_stats['audited']} audited  "
        f"| {audit_stats['unreachable']} unreachable  "
        f"| {audit_stats['errors']} errors"
    )

    # ── Stage 3: Score ────────────────────────────────────────────────────────
    _section("Stage 3 — Score")
    from scoring import score_all
    score_stats = score_all(verbose=False)
    print(
        f"\n  → {score_stats['qualified']} qualified  "
        f"| {score_stats['disqualified']} disqualified  "
        f"| {score_stats['total']} total"
    )

    # ── Stage 4: Enrich ───────────────────────────────────────────────────────
    _section("Stage 4 — Enrich contacts")
    from enrich_contacts import enrich_all, export_no_website_csv
    enrich_all(workers=4)
    export_no_website_csv(str(_WA_CSV))

    from database import _conn
    with _conn() as c:
        r = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses "
            "WHERE status='qualified' AND no_website=0 "
            "AND email IS NOT NULL AND email != ''"
        ).fetchone()
        enrich_counts["found"] = r["n"] if r else 0

    # ── Stage 5: Draft ────────────────────────────────────────────────────────
    if not openai_key:
        print("\n  WARNING: OPENAI_API_KEY not set — skipping draft generation")
    else:
        _section("Stage 5 — Write email drafts")
        from write_emails import write_emails, _init_drafts
        _init_drafts()
        write_emails(api_key=openai_key)

        from database import _conn
        with _conn() as c:
            r = c.execute(
                "SELECT COUNT(*) AS n FROM email_drafts WHERE status='draft'"
            ).fetchone()
            draft_count = r["n"] if r else 0

    # ── Follow-ups ────────────────────────────────────────────────────────────
    _section("Follow-ups")
    if openai_key:
        from write_emails import write_followup_drafts
        write_followup_drafts(api_key=openai_key)
    else:
        print("  Skipping (no OPENAI_API_KEY)")

    # ── Summary ───────────────────────────────────────────────────────────────
    _section("Daily Summary")
    if not no_harvest:
        print(f"  Harvested    : {harvest_stats['added']} new businesses")
    print(
        f"  Audited      : {audit_stats['audited']}  "
        f"({audit_stats['unreachable']} unreachable, {audit_stats['errors']} errors)"
    )
    print(
        f"  Scored       : {score_stats['qualified']} qualified  "
        f"| {score_stats['disqualified']} disqualified"
    )
    print(f"  Emails found : {enrich_counts['found']} website leads with email")
    print(f"  Drafts ready : {draft_count} pending in email_drafts")
    print(f"  WA CSV       : {_WA_CSV}")
    print()
    print("  ✅ Pipeline done — NO emails were sent.")
    print("  📊 Open the dashboard to review and approve:")
    print("       streamlit run dashboard.py")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
