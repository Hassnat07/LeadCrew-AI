"""
Stage 2 CLI — audit all harvested businesses.

Usage:
    python audit_runner.py            # audit all status='harvested' with a website
    python audit_runner.py --limit 20 # cap at 20 for a quick test run

Output: summary table  name | issues found | top evidence
Results saved to DB immediately after each site (safe to Ctrl-C and resume).

Sites that cannot be reached (timeout/DNS) keep status='harvested' so they
are retried on the next run — important on slow / filtered connections.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from database import get_businesses, update_business_by_id
from website_auditor import audit_site

PAGESPEED_KEY = os.getenv("PAGESPEED_API_KEY", "")
MAX_WORKERS = 8


# ── Worker ────────────────────────────────────────────────────────────────────

def _audit_one(biz: dict) -> dict:
    """Run all checks for one business. Never raises."""
    t0 = time.monotonic()
    url = (biz.get("website") or "").strip()
    if not url:
        biz["_skip"] = "no website"
        return biz

    try:
        checks = audit_site(
            url=url,
            biz_name=biz.get("name", ""),
            category=biz.get("category", ""),
            domain=biz.get("domain", ""),
            pagespeed_api_key=PAGESPEED_KEY,
        )
    except Exception as exc:
        biz["_error"] = str(exc)[:120]
        return biz

    biz["_checks"] = checks
    biz["_elapsed"] = round(time.monotonic() - t0, 1)
    return biz


# ── Persist one result ────────────────────────────────────────────────────────

def _save_result(biz: dict) -> list[tuple[str, str]]:
    """
    Write audit outcome to DB. Returns list of (key, evidence) for triggered checks.
    Returns None if the site was unreachable (status kept as 'harvested').
    """
    checks: dict = biz.get("_checks", {})

    if not checks:
        return []

    # site_unreachable → do NOT change status; will be retried next run
    if checks.get("site_unreachable", (False, ""))[0]:
        return []

    triggered = [
        (key, ev)
        for key, (trig, ev) in checks.items()
        if trig
    ]

    issues_json = json.dumps(triggered)
    update_business_by_id(
        biz["id"],
        issues_json=issues_json,
        audited_at=datetime.now().isoformat(),
        status="audited",
    )
    return triggered


# ── Summary table ─────────────────────────────────────────────────────────────

def _print_table(rows: list[dict]) -> None:
    rows_sorted = sorted(rows, key=lambda r: -len(r.get("_triggered", [])))
    W_NAME = 38
    W_CNT  = 6
    W_EV   = 60
    sep = f"{'─' * W_NAME}─{'─' * W_CNT}──{'─' * W_EV}"
    print(f"\n{sep}")
    print(
        f"{'BUSINESS NAME':<{W_NAME}} {'ISSUES':>{W_CNT}}  "
        f"{'TOP EVIDENCE':<{W_EV}}"
    )
    print(sep)
    for r in rows_sorted:
        name = (r.get("name") or "")[:W_NAME]
        triggered = r.get("_triggered", [])
        count = len(triggered)
        top_ev = (triggered[0][1] if triggered else r.get("_skip") or r.get("_error") or "—")[
            :W_EV
        ]
        flag = ""
        if r.get("_error"):
            flag = " [ERR]"
        elif checks := r.get("_checks", {}):
            if checks.get("site_unreachable", (False, ""))[0]:
                flag = " [UNREACHABLE]"
        print(f"{name:<{W_NAME}} {count:>{W_CNT}}{flag}  {top_ev:<{W_EV}}")
    print(sep)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Parse simple CLI flags
    limit = None
    args = sys.argv[1:]
    if "--limit" in args:
        idx = args.index("--limit")
        try:
            limit = int(args[idx + 1])
        except (IndexError, ValueError):
            print("Usage: python audit_runner.py [--limit N]")
            sys.exit(1)

    businesses = [
        b for b in get_businesses(status="harvested")
        if b.get("website") and not b.get("no_website")
    ]

    if limit:
        businesses = businesses[:limit]

    if not businesses:
        print("No harvested businesses with websites found.")
        print("Run harvester.py first, e.g.:")
        print('  python harvester.py "dentists" "Manchester, UK"')
        return

    total = len(businesses)
    print(f"Auditing {total} site(s)  ·  {MAX_WORKERS} parallel workers")
    if PAGESPEED_KEY:
        print("  PageSpeed API key: set  (higher rate limit)")
    else:
        print("  PageSpeed API key: not set  (25 free calls/day shared limit)")
    print()

    summary_rows: list[dict] = []
    done = 0
    unreachable = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_audit_one, biz): biz for biz in businesses}

        for fut in as_completed(futures):
            done += 1
            biz = futures[fut]
            name = (biz.get("name") or "?")[:38]

            try:
                result = fut.result()
            except Exception as exc:
                print(f"  [{done}/{total}] {name}  ERROR: {exc}")
                errors += 1
                summary_rows.append({**biz, "_triggered": [], "_error": str(exc)})
                continue

            triggered = _save_result(result)

            checks = result.get("_checks", {})
            if checks.get("site_unreachable", (False, ""))[0]:
                ev = checks["site_unreachable"][1]
                print(f"  [{done}/{total}] {name}  UNREACHABLE — {ev}")
                unreachable += 1
                summary_rows.append({**result, "_triggered": []})
                continue

            if result.get("_error"):
                print(f"  [{done}/{total}] {name}  ERROR: {result['_error']}")
                errors += 1
                summary_rows.append({**result, "_triggered": []})
                continue

            elapsed = result.get("_elapsed", 0)
            n = len(triggered)
            top = f"{triggered[0][0]}: {triggered[0][1][:50]}" if triggered else "no issues"
            print(f"  [{done}/{total}] {name}  {n} issues  ({elapsed}s)  {top}")

            summary_rows.append({**result, "_triggered": triggered})

    _print_table(summary_rows)

    audited    = done - unreachable - errors
    with_issues = sum(1 for r in summary_rows if r.get("_triggered"))
    all_issues  = sum(len(r.get("_triggered", [])) for r in summary_rows)

    print(
        f"\n  {done} sites processed — "
        f"{audited} audited, {unreachable} unreachable (will retry), {errors} errors"
    )
    print(
        f"  {with_issues} sites have ≥1 issue  ·  {all_issues} total issues found"
    )
    print("\n  Next step: run scoring.py to see qualified leads (pain_score ≥ 40).")


# ── Importable batch function (used by run_pipeline.py) ──────────────────────

def run_audit(
    businesses: list[dict] | None = None,
    limit: int | None = None,
) -> dict:
    """
    Audit a list of businesses (or all status='harvested' with a website if None).
    Saves results to DB as they complete. Returns stats dict.
    No print output — caller handles progress reporting.
    """
    if businesses is None:
        businesses = [
            b for b in get_businesses(status="harvested")
            if b.get("website") and not b.get("no_website")
        ]
    if limit:
        businesses = businesses[:limit]

    if not businesses:
        return {"total": 0, "audited": 0, "unreachable": 0, "errors": 0}

    total = len(businesses)
    audited = unreachable = errors = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_audit_one, biz): biz for biz in businesses}
        done = 0
        for fut in as_completed(futures):
            done += 1
            biz = futures[fut]
            name = (biz.get("name") or "?")[:38]

            try:
                result = fut.result()
            except Exception as exc:
                errors += 1
                print(f"    [{done}/{total}] {name}  ERROR: {exc}")
                continue

            triggered = _save_result(result)
            checks    = result.get("_checks", {})

            if checks.get("site_unreachable", (False, ""))[0]:
                ev = checks["site_unreachable"][1]
                print(f"    [{done}/{total}] {name}  UNREACHABLE — {ev}")
                unreachable += 1
            elif result.get("_error"):
                print(f"    [{done}/{total}] {name}  ERROR: {result['_error']}")
                errors += 1
            else:
                n       = len(triggered)
                elapsed = result.get("_elapsed", 0)
                top     = triggered[0][0] if triggered else "clean"
                print(f"    [{done}/{total}] {name}  {n} issues  ({elapsed}s)  [{top}]")
                audited += 1

    return {"total": total, "audited": audited, "unreachable": unreachable, "errors": errors}


if __name__ == "__main__":
    main()
