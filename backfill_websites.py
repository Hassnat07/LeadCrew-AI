"""
Re-query Serper Places for every no_website=1 lead and recover:
  - website field  (if Places now has one → reset to status='harvested' for re-audit)
  - phoneNumber    (normalize to E.164 and save if currently phone=NULL)
  - social link    (if GMB points at facebook/instagram, store in social_links)

Conflict detection (--apply):
  If a recovered domain is already owned by a DIFFERENT row in the DB, the
  website is NOT assigned.  Listed as CONFLICT for manual review.

Duplicate merging (--apply):
  If two no_website leads resolve to the same domain, the row with more
  reviews (higher rating_count, then lower id) is kept; the other is deleted.

Usage:
    python backfill_websites.py              # dry run — print what would change
    python backfill_websites.py --apply      # commit to DB
    python backfill_websites.py --workers 6  # parallelism (default 4)
"""

import json
import os
import sys
import threading
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from database import _init, _conn
from harvester import (
    _get_session, PLACES_URL,
    _extract_domain, _is_directory_url, _is_social_domain,
)
from phone_hunt import _to_e164, _infer_country
from runner import run_parallel


# ── Places query ──────────────────────────────────────────────────────────────

def _query_places(name: str, city: str, api_key: str) -> list[dict]:
    try:
        r = _get_session().post(
            PLACES_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": f"{name} {city}", "num": 5},
            timeout=(5, 20),
        )
        r.raise_for_status()
        return r.json().get("places", [])
    except Exception:
        return []


def _best_match(places: list[dict], name: str) -> dict | None:
    """Return the Places result most likely to match this business name."""
    name_lo = name.lower()
    stub    = name_lo[:8]
    for p in places:
        title = (p.get("title") or "").lower()
        if stub in title or title[:8] in name_lo[:12]:
            return p
    return places[0] if places else None


# ── social_links JSON-array merge ─────────────────────────────────────────────

def _merge_social(existing_json: str | None, new_url: str) -> str:
    try:
        links: list[str] = json.loads(existing_json) if existing_json else []
    except Exception:
        links = [existing_json] if existing_json else []
    if new_url not in links:
        links.append(new_url)
    return json.dumps(links)


# ── Query phase (runs in parallel via runner.run_parallel) ────────────────────

def _run_query_phase(
    targets: list[dict],
    api_key: str,
    workers: int,
) -> list[dict]:
    """
    Query Serper Places for all targets.  Returns list of change-dicts.
    Each dict: {id, name, website, domain, social_url, phone, existing_social}.
    """
    changes: list[dict] = []
    lock    = threading.Lock()

    print(f"{'Business':<45} {'Action':<52} {'Phone'}")
    print("─" * 112)

    def process(biz: dict) -> None:
        name = biz["name"]
        city = biz["city"]

        places = _query_places(name, city, api_key)
        match  = _best_match(places, name)

        if not match:
            with lock:
                print(f"  {name[:44]:<45} {'no Places match':<52}")
            return

        raw_web   = (match.get("website")     or "").strip()
        raw_phone = (match.get("phoneNumber") or "").strip()

        website    = None
        domain     = None
        social_url = None

        if raw_web:
            dom = _extract_domain(raw_web)
            if dom:
                if _is_social_domain(dom):
                    social_url = raw_web
                elif not _is_directory_url(raw_web):
                    website = raw_web
                    domain  = dom

        phone = None
        if raw_phone and not biz.get("phone"):
            phone = _to_e164(raw_phone, _infer_country(biz))

        with lock:
            if website:
                action = f"website → {website[:47]}"
            elif social_url:
                action = f"social  → {social_url[:47]}"
            else:
                action = "no website"
            print(f"  {name[:44]:<45} {action:<52} {phone or '—'}")

            if website or social_url or phone:
                changes.append({
                    "id":              biz["id"],
                    "name":            name,
                    "website":         website,
                    "domain":          domain,
                    "social_url":      social_url,
                    "phone":           phone,
                    "existing_social": biz.get("social_links"),
                })

    def on_error(biz: dict, exc: Exception) -> None:
        with lock:
            print(f"  {biz['name'][:44]:<45} {'ERROR':<52} {exc}")

    run_parallel(process, targets, workers=workers, on_error=on_error)
    print("─" * 112)
    return changes


# ── Apply phase (sequential, single-threaded) ─────────────────────────────────

def _apply_changes(
    changes: list[dict],
) -> tuple[int, int, list[tuple], list[tuple], list[int]]:
    """
    Apply changes with in-batch duplicate merging and DB conflict detection.

    Returns:
        (applied_web, applied_phone, merges, conflicts, deleted_ids)

    merges   — list of (keep_id, keep_name, drop_id, drop_name, domain)
    conflicts — list of (change_dict, existing_owner_name, existing_owner_id)
    deleted_ids — row ids deleted as duplicate losers
    """

    # ── Step A: within-batch duplicate domain resolution ─────────────────────
    # If two no_website leads both resolve to the same domain, merge them:
    # keep the row with higher rating_count (then lower id); delete the other.
    by_domain: dict[str, list[dict]] = {}
    no_domain:  list[dict]           = []

    for ch in changes:
        if ch["domain"]:
            by_domain.setdefault(ch["domain"], []).append(ch)
        else:
            no_domain.append(ch)

    to_apply:  list[dict] = list(no_domain)
    merges:    list[tuple] = []
    to_delete: list[int]   = []

    for domain, chs in by_domain.items():
        if len(chs) == 1:
            to_apply.append(chs[0])
            continue

        # Load rows for rating_count comparison
        ids = [ch["id"] for ch in chs]
        ph  = ",".join("?" * len(ids))
        with _conn() as c:
            rows = [
                dict(r)
                for r in c.execute(
                    f"SELECT id, name, rating_count "
                    f"FROM seen_businesses WHERE id IN ({ph})",
                    ids,
                ).fetchall()
            ]
        # Sort: higher rating_count wins; tie → lower id wins (older row)
        rows.sort(key=lambda r: (-(r.get("rating_count") or 0), r["id"]))

        keep_row = rows[0]
        keep_ch  = next(ch for ch in chs if ch["id"] == keep_row["id"])
        to_apply.append(keep_ch)

        for drop_row in rows[1:]:
            to_delete.append(drop_row["id"])
            merges.append((
                keep_row["id"], keep_row["name"],
                drop_row["id"], drop_row["name"],
                domain,
            ))

    # ── Step B: domain conflict check against existing DB rows ───────────────
    # A recovered domain that already belongs to ANOTHER business in the DB
    # cannot be assigned — that would break the unique index and mis-attribute
    # audit results.
    conflicts:     list[tuple] = []
    final_changes: list[dict]  = []

    for ch in to_apply:
        if not ch.get("domain"):
            final_changes.append(ch)
            continue

        with _conn() as c:
            existing = c.execute(
                "SELECT id, name FROM seen_businesses "
                "WHERE domain = ? AND id != ? LIMIT 1",
                (ch["domain"], ch["id"]),
            ).fetchone()

        if existing:
            conflicts.append((ch, existing["name"], existing["id"]))
            # Downgrade to phone/social-only — don't assign the conflicting domain
            final_changes.append({**ch, "website": None, "domain": None})
        else:
            final_changes.append(ch)

    # ── Step C: write updates ─────────────────────────────────────────────────
    applied_web   = 0
    applied_phone = 0

    for ch in final_changes:
        updates: dict[str, Any] = {}

        if ch.get("website") and ch.get("domain"):
            updates["website"]        = ch["website"]
            updates["domain"]         = ch["domain"]
            updates["website_source"] = "places"
            updates["no_website"]     = 0
            updates["status"]         = "harvested"
            applied_web += 1

        if ch.get("social_url"):
            updates["social_links"] = _merge_social(
                ch["existing_social"], ch["social_url"]
            )

        if ch.get("phone"):
            updates["phone"]        = ch["phone"]
            updates["phone_source"] = "places_backfill"
            applied_phone += 1

        if not updates:
            continue

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        try:
            with _conn() as c:
                c.execute(
                    f"UPDATE seen_businesses SET {set_clause} WHERE id = ?",
                    [*updates.values(), ch["id"]],
                )
        except Exception as exc:
            print(f"  [DB error] id={ch['id']}: {exc}")

    # Delete rows that lost the in-batch dedup merge
    if to_delete:
        ph = ",".join("?" * len(to_delete))
        with _conn() as c:
            c.execute(
                f"DELETE FROM seen_businesses WHERE id IN ({ph})", to_delete
            )

    return applied_web, applied_phone, merges, conflicts, to_delete


# ── Main orchestrator ─────────────────────────────────────────────────────────

def backfill(apply: bool = False, workers: int = 4, api_key: str = "") -> None:
    _init()

    # Before counts
    with _conn() as c:
        before_no_web = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses "
            "WHERE no_website = 1 AND status NOT IN ('excluded', 'disqualified')"
        ).fetchone()["n"]
        before_phone = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses "
            "WHERE phone IS NOT NULL AND phone != ''"
        ).fetchone()["n"]

    print(f"\n{'='*65}")
    print("BACKFILL — Places website + phone recovery for no_website leads")
    print(f"{'='*65}")
    print(f"Mode                : {'APPLY' if apply else 'DRY RUN (add --apply to commit)'}")
    print(f"no_website leads    : {before_no_web}")
    print(f"Phones in DB before : {before_phone}")
    print()

    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, city, phone, domain, address, social_links "
            "FROM seen_businesses "
            "WHERE no_website = 1 AND status NOT IN ('excluded', 'disqualified') "
            "ORDER BY id"
        ).fetchall()
    targets = [dict(r) for r in rows]

    if not targets:
        print("No no_website leads to process.")
        return

    # ── Query phase (parallel) ────────────────────────────────────────────────
    changes = _run_query_phase(targets, api_key, workers)

    found_website = sum(1 for ch in changes if ch.get("website"))
    found_social  = sum(1 for ch in changes if ch.get("social_url"))
    found_phone   = sum(1 for ch in changes if ch.get("phone"))

    print(f"\nDry-run summary ({len(targets)} no_website leads queried):")
    print(f"  Recoverable website (→ re-audit) : {found_website}")
    print(f"  Social link only (no real site)  : {found_social}")
    print(f"  Phone found                      : {found_phone}")
    print(f"  No change                        : {len(targets) - len(changes)}")

    if not apply:
        print(f"\nRe-run with --apply to commit changes to the DB.")
        return

    # ── Apply phase (sequential) ──────────────────────────────────────────────
    applied_web, applied_phone, merges, conflicts, deleted_ids = _apply_changes(changes)

    # ── Merges report ─────────────────────────────────────────────────────────
    if merges:
        print(f"\nMERGES — {len(merges)} duplicate pair(s) resolved:")
        for keep_id, keep_name, drop_id, drop_name, domain in merges:
            print(f"  KEPT  id={keep_id:<4} {keep_name[:45]!r}")
            print(f"  DROP  id={drop_id:<4} {drop_name[:45]!r}  → {domain}")
            print()

    # ── Conflicts report ──────────────────────────────────────────────────────
    if conflicts:
        print(
            f"\nCONFLICTS — {len(conflicts)} domain(s) already owned by another business:"
        )
        print("  Website NOT assigned; leads remain no_website=1. Review manually.\n")
        print(
            f"  {'Lead':<42} {'Recovered domain':<36} Already owned by"
        )
        print("  " + "─" * 100)
        for ch, owner_name, owner_id in conflicts:
            print(
                f"  {ch['name'][:41]:<42} {ch['domain']:<36}"
                f" {owner_name[:35]} (id={owner_id})"
            )

    # ── After funnel counts ───────────────────────────────────────────────────
    with _conn() as c:
        total      = c.execute("SELECT COUNT(*) AS n FROM seen_businesses").fetchone()["n"]
        with_web   = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses WHERE no_website = 0"
        ).fetchone()["n"]
        no_web_act = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses "
            "WHERE no_website = 1 AND status NOT IN ('excluded', 'disqualified')"
        ).fetchone()["n"]
        with_phone = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses "
            "WHERE phone IS NOT NULL AND phone != ''"
        ).fetchone()["n"]
        reharvest  = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses WHERE status = 'harvested'"
        ).fetchone()["n"]

    print(f"\nApplied:")
    print(f"  Websites restored (→ status=harvested) : {applied_web}")
    print(f"  Phones added                           : {applied_phone}")
    print(f"  Duplicate rows deleted                 : {len(deleted_ids)}")

    print(f"\n{'='*55}")
    print("FUNNEL AFTER BACKFILL")
    print(f"{'='*55}")
    print(f"  Total                          : {total}")
    print(f"  Has website   (no_website=0)   : {with_web}")
    print(f"  Genuinely no website (active)  : {no_web_act}")
    print(f"  Has phone                      : {with_phone}")
    print(f"  Pending re-audit  (harvested)  : {reharvest}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _arg(args: list[str], flag: str, default=None):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return default


if __name__ == "__main__":
    api_key = os.getenv("SERPER_API_KEY", "")
    if not api_key:
        print("ERROR: SERPER_API_KEY not set.")
        sys.exit(1)

    args        = sys.argv[1:]
    apply_flag  = "--apply" in args
    workers_arg = int(_arg(args, "--workers")) if "--workers" in args else 4

    backfill(apply=apply_flag, workers=workers_arg, api_key=api_key)
