"""
Full pipeline CLI -- Stage 1 -> 2 -> 3 in one command.

Usage:
    python run_pipeline.py "dentists" "Manchester, UK"
    python run_pipeline.py "plumbers" "London, UK" [--no-harvest]   # skip harvest
    python run_pipeline.py --score-only                              # rescore audited leads

Flags:
    --no-harvest   Skip Stage 1; audit + score whatever is already harvested.
    --score-only   Skip Stages 1 & 2; just (re)score all audited leads.

Output at the end: qualified leads sorted by pain_score, top 3 evidence lines each.
This is the human review milestone before any email code is written.
"""
import json
import os
import sys
from datetime import date

from dotenv import load_dotenv

load_dotenv()


# -- Helpers -------------------------------------------------------------------

def _divider(char: str = "=", width: int = 78) -> str:
    return char * width


def _print_qualified(leads: list[dict]) -> None:
    if not leads:
        print("\n  No qualified leads found yet.")
        print("  Possible reasons:")
        print("  - pain_score < 40 for all sites (lower threshold in scoring.py to debug)")
        print("  - No site passed the success filter (rating_count < 20 AND rating < 4.0)")
        print("  - All sites were unreachable -- try again on a better connection")
        return

    print(f"\n{_divider()}")
    print(
        f"  QUALIFIED LEADS  |  {len(leads)} lead(s)  |  "
        f"sorted by pain score  |  {date.today()}"
    )
    print(_divider())

    for rank, biz in enumerate(leads, 1):
        name         = biz.get("name") or "Unknown"
        city         = biz.get("city") or ""
        pain_score   = biz.get("pain_score") or 0
        rating       = biz.get("rating") or 0.0
        rating_count = biz.get("rating_count") or 0
        website      = biz.get("website") or "(no website)"
        category     = biz.get("category") or ""
        no_web       = biz.get("no_website")
        grade        = biz.get("grade") or ""

        star_str  = f"*{rating:.1f}" if rating else "no rating"
        rev_str   = f"({rating_count} reviews)" if rating_count else ""
        grade_str = f"  [{grade}]" if grade else ""

        print(f"\n  #{rank}  {name}  |  {city}")
        if category:
            print(f"       Category   : {category}")
        print(f"       Pain score : {pain_score}/100{grade_str}   {star_str} {rev_str}")
        if no_web:
            print(f"       Website    : NONE -- route to phone/WhatsApp campaign")
        else:
            print(f"       Website    : {website}")

        # Top 3 evidence lines from issues_json
        try:
            issues = json.loads(biz.get("issues_json") or "[]")
        except Exception:
            issues = []

        top3 = issues[:3]
        if top3:
            for i, item in enumerate(top3, 1):
                if len(item) == 3:
                    key, weight, evidence = item
                    print(f"       {i}. [{key:<22} {weight:>3}pts]  {evidence}")
                elif len(item) == 2:
                    key, evidence = item
                    print(f"       {i}. [{key:<22}     ]  {evidence}")
        else:
            print("       (no evidence recorded)")

    print(f"\n{_divider()}")


def _section(title: str) -> None:
    print(f"\n{'-' * 78}")
    print(f"  {title}")
    print(f"{'-' * 78}")


# -- Main ----------------------------------------------------------------------

def main() -> None:
    args       = sys.argv[1:]
    no_harvest = "--no-harvest" in args
    score_only = "--score-only" in args
    args       = [a for a in args if not a.startswith("--")]

    if not score_only and not no_harvest and len(args) < 2:
        print(__doc__)
        sys.exit(1)

    niche = args[0] if len(args) > 0 else ""
    city  = args[1] if len(args) > 1 else ""

    api_key = os.getenv("SERPER_API_KEY", "")
    if not api_key and not score_only:
        print("ERROR: SERPER_API_KEY not set in .env")
        sys.exit(1)

    harvest_stats = {"added": 0}
    audit_stats   = {"total": 0, "audited": 0, "unreachable": 0, "errors": 0}
    score_stats   = {"total": 0, "qualified": 0, "disqualified": 0}

    # -- Stage 1: Harvest ------------------------------------------------------
    if not score_only and not no_harvest:
        _section(f"STAGE 1 -- Harvest  |  {niche} in {city}")
        from harvester import harvest
        added = harvest(niche, city, api_key)
        harvest_stats["added"] = len(added)
        print(f"\n  -> {len(added)} new businesses saved to DB")
    else:
        if score_only:
            print("  [--score-only] Skipping harvest and audit.")
        else:
            print("  [--no-harvest] Skipping harvest.")

    # -- Stage 2: Audit --------------------------------------------------------
    if not score_only:
        _section("STAGE 2 -- Audit  (all harvested sites with a website)")
        from audit_runner import run_audit
        audit_stats = run_audit()
        print(
            f"\n  -> {audit_stats['audited']} audited, "
            f"{audit_stats['unreachable']} unreachable (will retry), "
            f"{audit_stats['errors']} errors"
        )

    # -- Stage 3: Score --------------------------------------------------------
    _section("STAGE 3 -- Score  (all audited sites + no_website leads)")
    from scoring import score_all
    score_stats = score_all(verbose=False)
    print(
        f"\n  -> {score_stats['total']} scored: "
        f"{score_stats['qualified']} qualified, "
        f"{score_stats['disqualified']} disqualified"
    )

    # -- Qualified leads report ------------------------------------------------
    from database import get_businesses
    qualified = get_businesses(status="qualified")
    qualified.sort(key=lambda b: (-(b.get("pain_score") or 0), -(b.get("rating_count") or 0)))

    _print_qualified(qualified)

    # -- Pipeline summary ------------------------------------------------------
    print("  PIPELINE SUMMARY")
    print(f"  {'-' * 50}")
    if not score_only and not no_harvest:
        print(f"  Stage 1  Harvested   : {harvest_stats['added']} new businesses")
    if not score_only:
        print(
            f"  Stage 2  Audited     : {audit_stats['audited']} sites  "
            f"({audit_stats['unreachable']} unreachable, {audit_stats['errors']} errors)"
        )
    print(
        f"  Stage 3  Scored      : {score_stats['total']}  ->  "
        f"{score_stats['qualified']} qualified  |  {score_stats['disqualified']} disqualified"
    )
    print(f"  {'-' * 50}")
    print(
        f"  Total qualified in DB: {len(qualified)}\n"
        f"\n  Review the leads above before proceeding to email writing (Stage 5)."
    )
    print(_divider())


if __name__ == "__main__":
    main()
