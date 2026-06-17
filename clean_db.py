"""
DB clean-up — removes poisoned data and merges duplicates.

Three passes:
  1. DIRECTORY / STAGING URLs — domain is a trade directory, review site,
     government registry, or staging subdomain.  The website field is wrong;
     we either re-run the website lookup (if SERPER_API_KEY is present) or
     reset these rows to no_website=1 so they become phone-campaign leads.

  2. WRONG-COUNTRY addresses — US state+ZIP pattern in address while the city
     query is not US.  These are foreign lookalikes (Manchester, IA harvested
     for Manchester, UK).  Deleted entirely — they cannot be contacted.

  3. DUPLICATES — same normalized name + city stored twice (happens when the
     same business appears in both a 'Manchester' run and a 'Manchester, UK'
     run).  Keeps the row with a verified website over the no_website row;
     when both have or both lack websites, keeps the lower id (older).

Pass 4:  Normalize the city column in all remaining rows (strip country suffix).

Usage:
    python clean_db.py             # preview changes (dry-run)
    python clean_db.py --apply     # apply changes to DB
    python clean_db.py --apply --relookup   # also re-try website search for
                                            # rows reset to no_website
"""
import os
import re
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


# ── Imports shared with harvester ─────────────────────────────────────────────
from harvester import (
    _is_directory_url,
    _normalize_city_for_db,
    _find_website,
    _extract_domain,
    DIRECTORY_BLOCKLIST,
)
from database import _init, _conn, update_business_by_id

_US_STATE_ZIP_RE = re.compile(r",\s*[A-Z]{2}\s+\d{5}(-\d{4})?")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _norm_city(city: str) -> str:
    return (city or "").split(",")[0].strip().lower()


def _is_us_address(address: str) -> bool:
    return bool(_US_STATE_ZIP_RE.search(address or ""))


def _target_is_us(city: str) -> bool:
    parts = city.split(",")
    if len(parts) < 2:
        return False
    suffix = parts[-1].strip().lower()
    return suffix in ("us", "usa", "united states", "america")


# ── Delete by domain ─────────────────────────────────────────────────────────

def delete_domain(domain: str) -> None:
    """Delete a business by domain and print what was removed."""
    _init()
    domain = domain.lower().strip()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM seen_businesses WHERE domain = ? LIMIT 1",
            (domain,),
        ).fetchone()
        if row is None:
            print(f"Not found in DB: {domain}")
            return
        row = dict(row)
        c.execute("DELETE FROM seen_businesses WHERE domain = ?", (domain,))
    print(
        f"Deleted  id={row['id']}  name={row['name']!r}  "
        f"domain={row['domain']}  city={row.get('city', '')}  "
        f"address={row.get('address', '')!r}"
    )


# ── Main clean routine ────────────────────────────────────────────────────────

def clean(apply: bool = False, relookup: bool = False) -> None:
    _init()
    serper_key = os.getenv("SERPER_API_KEY", "")
    if relookup and not serper_key:
        print("WARNING: --relookup requires SERPER_API_KEY in .env — skipping re-lookup.")
        relookup = False

    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM seen_businesses ORDER BY id").fetchall()]

    total_before = len(rows)
    print(f"\nBEFORE: {total_before} businesses in DB")

    # ── Pass 1: directory / staging URLs ──────────────────────────────────────
    # Build a lookup of (normalized_name, normalized_city) → ids for valid rows
    # so we can detect whether a directory row has a valid counterpart.
    valid_name_city: dict[tuple, list[int]] = {}
    for r in rows:
        if not (r.get("domain") or "").strip() or r.get("no_website"):
            continue
        key = (_norm_name(r.get("name", "")), _norm_city(r.get("city", "")))
        valid_name_city.setdefault(key, []).append(r["id"])

    dir_ids: list[int]   = []   # directory rows to reset or delete
    dir_rows: list[dict] = []
    dir_delete: set[int] = set()  # subset of dir_ids to DELETE (valid dup exists)

    for r in rows:
        if r.get("no_website"):
            continue
        domain  = r.get("domain") or ""
        website = r.get("website") or ""
        if _is_directory_url(website) or _is_directory_url("https://" + domain if domain else ""):
            dir_ids.append(r["id"])
            dir_rows.append(r)
            # If a valid (non-directory) record with same name+city already
            # exists, delete this row outright instead of resetting it.
            key = (_norm_name(r.get("name", "")), _norm_city(r.get("city", "")))
            valid_others = [i for i in valid_name_city.get(key, []) if i != r["id"]]
            if valid_others:
                dir_delete.add(r["id"])

    dir_reset = [r for r in dir_rows if r["id"] not in dir_delete]

    print(f"\n  Pass 1 — directory/staging URLs:  {len(dir_ids)} row(s)")
    for r in dir_rows:
        action = "DELETE (valid dup exists)" if r["id"] in dir_delete else "reset to no_website=1"
        print(f"    id={r['id']:3}  {r['name'][:40]:<40}  {action}")

    # ── Pass 2: wrong-country addresses ───────────────────────────────────────
    foreign_ids: list[int] = []
    for r in rows:
        if r["id"] in dir_ids:
            continue  # already handled
        addr = r.get("address") or ""
        city = r.get("city") or ""
        if _is_us_address(addr) and not _target_is_us(city):
            foreign_ids.append(r["id"])

    print(f"\n  Pass 2 — wrong-country addresses: {len(foreign_ids)} row(s)")
    for r in rows:
        if r["id"] in foreign_ids:
            print(f"    id={r['id']:3}  {r['name'][:45]:<45}  addr={r.get('address','')[:60]}")

    # ── Pass 3: duplicates (same normalized name + city) ──────────────────────
    # Build groups — exclude rows being DELETED (dir_delete + foreign), but
    # INCLUDE dir_reset rows (they've been changed to no_website=1 virtually)
    # so they can be deduped against original no_website rows.
    skip_ids   = dir_delete | set(foreign_ids)
    live_rows  = [r for r in rows if r["id"] not in skip_ids]
    # Virtually apply the reset for dir_reset rows so dedup sees them as no_website=1
    def _effective_no_website(r: dict) -> int:
        return 1 if r["id"] in {x["id"] for x in dir_reset} else (r.get("no_website") or 0)

    groups: dict[tuple, list[dict]] = {}
    for r in live_rows:
        key = (_norm_name(r.get("name", "")), _norm_city(r.get("city", "")))
        groups.setdefault(key, []).append(r)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    delete_ids: list[int] = []
    merge_log:  list[str] = []

    for (name_key, city_key), group in dup_groups.items():
        # Sort: rows with a valid website first (effectively), then by id ascending
        group_sorted = sorted(
            group,
            key=lambda r: (1 if (_effective_no_website(r) or not r.get("domain")) else 0, r["id"]),
        )
        keep = group_sorted[0]
        for drop in group_sorted[1:]:
            delete_ids.append(drop["id"])
            merge_log.append(
                f"    KEEP id={keep['id']} ({keep.get('domain') or 'no_website'})  "
                f"DROP id={drop['id']} ({drop.get('domain') or 'no_website'})  "
                f"-> {name_key!r} / {city_key!r}"
            )

    print(f"\n  Pass 3 — duplicates:              {len(delete_ids)} row(s) to drop")
    for line in merge_log:
        print(line)

    # ── Pass 4: city normalization ─────────────────────────────────────────────
    normalize_needed: list[tuple[int, str]] = []  # (id, new_city)
    all_remaining_ids = set(r["id"] for r in live_rows) - set(delete_ids)
    for r in live_rows:
        if r["id"] not in all_remaining_ids:
            continue
        norm = _normalize_city_for_db(r.get("city") or "")
        if norm != (r.get("city") or ""):
            normalize_needed.append((r["id"], norm))

    print(f"\n  Pass 4 — city normalization:      {len(normalize_needed)} row(s)")
    for bid, new_city in normalize_needed[:10]:
        old = next(r.get("city") for r in rows if r["id"] == bid)
        print(f"    id={bid:3}  '{old}' -> '{new_city}'")
    if len(normalize_needed) > 10:
        print(f"    ... and {len(normalize_needed) - 10} more")

    # ── Summary ───────────────────────────────────────────────────────────────
    # dir_delete rows are deleted; dir_reset rows stay (as no_website=1)
    total_removed  = len(dir_delete) + len(foreign_ids) + len(delete_ids)
    total_after    = total_before - total_removed
    print(f"\n{'-'*60}")
    print(f"  Summary  (apply={apply})")
    print(f"{'-'*60}")
    print(f"  Directory rows to delete (dup) : {len(dir_delete)}")
    print(f"  Directory rows to reset (keep) : {len(dir_reset)}")
    print(f"  Wrong-country rows to delete  : {len(foreign_ids)}")
    print(f"  Duplicate rows to drop        : {len(delete_ids)}")
    print(f"  City fields to normalize      : {len(normalize_needed)}")
    print(f"  BEFORE -> AFTER               : {total_before} -> {total_after}")

    if not apply:
        print(f"\n  [DRY RUN] Re-run with --apply to commit changes.")
        return

    # ── Apply ─────────────────────────────────────────────────────────────────
    print("\n  Applying changes...")

    with _conn() as c:
        # Pass 1a: delete directory rows that have a valid counterpart
        if dir_delete:
            placeholders = ",".join("?" * len(dir_delete))
            c.execute(
                f"DELETE FROM seen_businesses WHERE id IN ({placeholders})",
                list(dir_delete),
            )
        print(f"    {len(dir_delete)} directory rows DELETED (valid dup exists)")

        # Pass 1b: reset remaining directory rows to no_website=1 (or re-lookup)
        relookup_results: list[str] = []
        for r in dir_reset:
            if relookup and serper_key:
                city_q = r.get("city") or ""
                url, src = _find_website(r["name"], city_q, serper_key)
                new_domain = _extract_domain(url) if url else None
                if new_domain and new_domain != r.get("domain"):
                    c.execute(
                        "UPDATE seen_businesses SET website=?, domain=?, no_website=0, "
                        "website_source=?, status='harvested', issues_json=NULL, "
                        "pain_score=NULL, audited_at=NULL WHERE id=?",
                        (url, new_domain, src, r["id"]),
                    )
                    relookup_results.append(
                        f"    id={r['id']} re-looked up -> {new_domain} [{src}]"
                    )
                    continue
            # Reset to no_website=1 (becomes phone-campaign lead)
            c.execute(
                "UPDATE seen_businesses SET website=NULL, domain=NULL, no_website=1, "
                "website_source=NULL, status='harvested', issues_json=NULL, "
                "pain_score=NULL, audited_at=NULL WHERE id=?",
                (r["id"],),
            )

        for line in relookup_results:
            print(line)
        print(
            f"    {len(dir_reset)} directory rows reset to no_website=1 "
            f"({len(relookup_results)} re-looked up)"
        )

        # Pass 2: delete wrong-country rows
        if foreign_ids:
            placeholders = ",".join("?" * len(foreign_ids))
            c.execute(f"DELETE FROM seen_businesses WHERE id IN ({placeholders})", foreign_ids)
        print(f"    {len(foreign_ids)} wrong-country rows deleted")

        # Pass 3: delete duplicate losers
        if delete_ids:
            placeholders = ",".join("?" * len(delete_ids))
            c.execute(f"DELETE FROM seen_businesses WHERE id IN ({placeholders})", delete_ids)
        print(f"    {len(delete_ids)} duplicate rows deleted")

        # Pass 4: normalize city
        for bid, new_city in normalize_needed:
            c.execute("UPDATE seen_businesses SET city=? WHERE id=?", (new_city, bid))
        print(f"    {len(normalize_needed)} city fields normalized")

    # After counts
    with _conn() as c:
        after_rows = c.execute("SELECT status, COUNT(*) as n FROM seen_businesses GROUP BY status").fetchall()
        total_after_actual = c.execute("SELECT COUNT(*) as n FROM seen_businesses").fetchone()["n"]

    print(f"\nAFTER: {total_after_actual} businesses in DB")
    for r in after_rows:
        print(f"  {r['status']:<14}: {r['n']}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args     = sys.argv[1:]
    apply    = "--apply"    in args
    relookup = "--relookup" in args

    if "--delete-domain" in args:
        idx = args.index("--delete-domain")
        if idx + 1 >= len(args):
            print("Usage: python clean_db.py --delete-domain <domain>")
            sys.exit(1)
        _init()
        delete_domain(args[idx + 1])
        sys.exit(0)

    clean(apply=apply, relookup=relookup)
