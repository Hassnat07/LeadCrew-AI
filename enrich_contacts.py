"""
Phase 4 — Contact enrichment (steps 1–4 of the 6-step ladder).

For every qualified business with a website, four pages are tried in order:
  1. homepage  (footer / header)
  2. /contact
  3. /contact-us
  4. /about  (then /about-us)

Emails are extracted only from actual page HTML — never guessed or constructed.
The first email found is saved together with the page it came from.

For no_website leads: export to a phone/WhatsApp CSV.

Usage:
    python enrich_contacts.py                       # enrich all qualified
    python enrich_contacts.py --limit 20            # cap at N businesses
    python enrich_contacts.py --csv out.csv         # also write no-website CSV
    python enrich_contacts.py --workers 6           # parallelism (default 4)
"""

import csv
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from database import _init, _conn


# ── HTTP helpers ──────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
_TIMEOUT = (5, 12)  # (connect, read) seconds

_thread_local = threading.local()


def _session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update(_HEADERS)
        s.max_redirects = 5
        _thread_local.session = s
    return _thread_local.session


def _fetch(url: str) -> str | None:
    """GET url; return HTML text or None on any error / non-HTML response."""
    try:
        r = _session().get(url, timeout=_TIMEOUT, allow_redirects=True)
        ct = r.headers.get("content-type", "")
        if r.status_code < 400 and "text/html" in ct:
            return r.text
    except Exception:
        pass
    return None


# ── Email extraction ──────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

_JUNK_DOMAINS = frozenset({
    "example.com", "example.org", "example.net",
    "sentry.io", "wixpress.com", "squarespace.com",
    "wix.com", "wordpress.com", "yourcompany.com",
    "yourdomain.com", "domain.com", "email.com",
    "acme.com", "test.com",
})

# Prefixes that are never real contact addresses
_JUNK_PREFIX_RE = re.compile(
    r"^(noreply|no-reply|donotreply|mailer-daemon|postmaster|webmaster|bounce|support-noreply)@",
    re.I,
)

# An "email" that's really an image filename embedded in a URL
_IMAGE_EXT_RE = re.compile(r"\.(png|jpg|jpeg|gif|svg|webp|ico|bmp|tiff)$", re.I)


def _is_junk(addr: str) -> bool:
    _, _, domain = addr.partition("@")
    domain = domain.lower()
    if not domain or "." not in domain:
        return True
    if domain in _JUNK_DOMAINS:
        return True
    if _JUNK_PREFIX_RE.match(addr):
        return True
    if _IMAGE_EXT_RE.search(domain):
        return True
    # PEC (Italian certified email) — wrong channel for cold outreach
    if domain.startswith("pec.") or ".pec." in domain or domain.endswith(".pec"):
        return True
    return False


def _extract_emails(html: str) -> list[str]:
    """
    Return emails literally present in the page HTML, deduplicated, junk-filtered.
    Priority: explicit mailto: href links, then visible page text.
    Never constructs or infers addresses.
    """
    found: list[str] = []
    seen: set[str] = set()

    soup = BeautifulSoup(html, "html.parser")

    # 1. mailto: links — most reliable source; captures obfuscated text too
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if href.lower().startswith("mailto:"):
            addr = unquote(href[7:].split("?")[0]).strip().lower().lstrip()
            if addr and addr not in seen and not _is_junk(addr):
                found.append(addr)
                seen.add(addr)

    # 2. Visible text scan — strip scripts/styles to avoid false positives
    for unwanted in soup(["script", "style", "noscript"]):
        unwanted.decompose()
    text = soup.get_text(" ", strip=True)
    for m in _EMAIL_RE.finditer(text):
        addr = m.group().lower()
        if addr not in seen and not _is_junk(addr):
            found.append(addr)
            seen.add(addr)

    return found


# ── Scraping ladder ───────────────────────────────────────────────────────────

# (label used in email_source column, path to append to base URL)
_LADDER: list[tuple[str, str]] = [
    ("homepage",    ""),
    ("contact",     "/contact"),
    ("contact-us",  "/contact-us"),
    ("about",       "/about"),
    ("about-us",    "/about-us"),
]


def _base_url(website: str, domain: str) -> str:
    """Derive scheme://host from the website field, or fall back to https://domain."""
    if website:
        p = urlparse(website)
        return f"{p.scheme}://{p.netloc}".rstrip("/")
    return f"https://{domain}"


def _scrape_ladder(website: str, domain: str) -> tuple[str, str] | tuple[None, None]:
    """
    Walk the 4-page ladder and return (email, source_label) from the first
    page that yields a real email.  Returns (None, None) if all pages fail.
    """
    base = _base_url(website, domain)

    for label, path in _LADDER:
        url = base + path
        html = _fetch(url)
        if not html:
            continue
        emails = _extract_emails(html)
        if emails:
            return emails[0], label

    return None, None


def _scrape_social_for_email(social_url: str) -> tuple[str, str] | tuple[None, None]:
    """Best-effort: scrape a social media profile page for a contact email.
    Facebook /about pages sometimes expose emails in plain HTML before login wall."""
    if not social_url:
        return None, None
    urls_to_try: list[tuple[str, str]] = [(social_url, "social_profile")]
    if "facebook.com" in social_url:
        base = social_url.rstrip("/")
        urls_to_try = [(f"{base}/about", "facebook_about"), (social_url, "facebook_page")]
    for url, label in urls_to_try:
        html = _fetch(url)
        if not html:
            continue
        emails = _extract_emails(html)
        if emails:
            return emails[0], label
    return None, None


# ── DB helpers ────────────────────────────────────────────────────────────────

def _migrate() -> None:
    """Ensure email / email_source columns exist (idempotent)."""
    with _conn() as c:
        for stmt in (
            "ALTER TABLE seen_businesses ADD COLUMN email TEXT",
            "ALTER TABLE seen_businesses ADD COLUMN email_source TEXT",
        ):
            try:
                c.execute(stmt)
            except Exception:
                pass


def _get_targets(limit: int | None) -> list[dict]:
    """Qualified businesses with a website that haven't been enriched yet."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, website, domain, pain_score "
            "FROM seen_businesses "
            "WHERE status = 'qualified' AND no_website = 0 "
            "  AND (email IS NULL OR email = '') "
            "ORDER BY pain_score DESC"
            + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()
        return [dict(r) for r in rows]


def _get_no_website_social_targets(limit: int | None) -> list[dict]:
    """No-website leads that have a social link but no email yet — try social scraping."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, social_links, pain_score "
            "FROM seen_businesses "
            "WHERE status = 'qualified' AND no_website = 1 "
            "  AND social_links IS NOT NULL AND social_links NOT IN ('', '[]') "
            "  AND (email IS NULL OR email = '') "
            "ORDER BY pain_score DESC"
            + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()
        return [dict(r) for r in rows]


def _save_contact(biz_id: int, email: str, source: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE seen_businesses SET email = ?, email_source = ? WHERE id = ?",
            (email.lower().strip(), source, biz_id),
        )


def _get_no_website_leads() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT name, phone, rating, rating_count, city, address, niche, category, "
            "grade, pain_score, social_links "
            "FROM seen_businesses "
            "WHERE no_website = 1 AND status = 'qualified' "
            "ORDER BY CASE grade WHEN 'A+' THEN 0 WHEN 'A' THEN 1 ELSE 2 END, "
            "pain_score DESC NULLS LAST"
        ).fetchall()
        return [dict(r) for r in rows]


# ── CSV export ────────────────────────────────────────────────────────────────

_CSV_FIELDS = [
    "grade", "pain_score", "name", "phone", "wa_link", "social",
    "rating", "rating_count", "city", "address", "niche", "category",
]


def export_no_website_csv(path: str) -> int:
    """Write no-website qualified leads to a grade-sorted WhatsApp CSV. Returns row count."""
    leads = _get_no_website_leads()
    if not leads:
        print("  No no-website qualified leads found.")
        return 0

    rows = []
    for lead in leads:
        phone = lead.get("phone") or ""
        wa_link = ""
        if phone.startswith("+"):
            wa_link = f"https://wa.me/{phone[1:]}"
        elif phone:
            wa_link = f"https://wa.me/{phone}"

        social = ""
        try:
            links = json.loads(lead.get("social_links") or "[]")
            social = links[0] if links else ""
        except Exception:
            pass

        rows.append({
            "grade":        lead.get("grade") or "",
            "pain_score":   lead.get("pain_score") or "",
            "name":         lead.get("name") or "",
            "phone":        phone,
            "wa_link":      wa_link,
            "social":       social,
            "rating":       lead.get("rating") or "",
            "rating_count": lead.get("rating_count") or "",
            "city":         lead.get("city") or "",
            "address":      lead.get("address") or "",
            "niche":        lead.get("niche") or "",
            "category":     lead.get("category") or "",
        })

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Exported {len(rows)} no-website leads -> {path}")
    return len(rows)


# ── Main enrichment loop ──────────────────────────────────────────────────────

def enrich_all(limit: int | None = None, workers: int = 4) -> None:
    _init()
    _migrate()

    targets = _get_targets(limit)
    total = len(targets)
    if total == 0:
        print("Nothing to enrich — no qualified businesses with websites are awaiting contact info.")
        return

    print(f"\nContact enrichment — {total} businesses, {workers} workers")
    print(f"\n{'#':<5} {'Business':<42} {'Email':<38} Source")
    print("─" * 100)

    found_count = 0
    miss_count  = 0
    lock        = threading.Lock()
    counter     = [0]

    def process(biz: dict) -> None:
        nonlocal found_count, miss_count
        name    = biz.get("name") or "?"
        website = biz.get("website") or ""
        domain  = biz.get("domain") or ""

        email, source = _scrape_ladder(website, domain)

        with lock:
            counter[0] += 1
            n = counter[0]
            if email:
                _save_contact(biz["id"], email, source)
                found_count += 1
                print(f"{n:<5} {name[:41]:<42} {email[:37]:<38} {source}")
            else:
                miss_count += 1
                print(f"{n:<5} {name[:41]:<42} {'—':<38} not found")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(process, b) for b in targets]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                with lock:
                    print(f"  [worker error] {exc}")

    print("─" * 100)
    print(f"\nDone (website enrichment): {found_count} found, {miss_count} not found")

    # Phase 2: try social profile scraping for no-website leads
    social_targets = _get_no_website_social_targets(limit)
    if social_targets:
        print(f"\nSocial-profile enrichment — {len(social_targets)} no-website leads")
        print(f"{'#':<5} {'Business':<42} {'Email':<38} Source")
        print("─" * 100)
        social_found = 0
        for i, biz in enumerate(social_targets, 1):
            try:
                links = json.loads(biz.get("social_links") or "[]")
                email, source = _scrape_social_for_email(links[0] if links else "")
            except Exception:
                email, source = None, None
            if email:
                _save_contact(biz["id"], email, source)
                social_found += 1
                print(f"{i:<5} {(biz.get('name') or '')[:41]:<42} {email[:37]:<38} {source}")
            else:
                print(f"{i:<5} {(biz.get('name') or '')[:41]:<42} {'—':<38} not found")
        print("─" * 100)
        print(f"Social enrichment: {social_found} emails found out of {len(social_targets)}")

    # Summary of DB state
    with _conn() as c:
        with_email = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses "
            "WHERE status = 'qualified' AND email IS NOT NULL AND email != ''"
        ).fetchone()["n"]
        no_web_no_email = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses "
            "WHERE no_website = 1 AND status = 'qualified' "
            "  AND (email IS NULL OR email = '')"
        ).fetchone()["n"]

    print(f"\nDB summary:")
    print(f"  Qualified leads with email    : {with_email}  → go to email queue")
    print(f"  No-website leads without email: {no_web_no_email}  → WhatsApp outreach")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _arg(args: list[str], flag: str, default=None):
    """Return the value after --flag, or default."""
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return default


if __name__ == "__main__":
    args = sys.argv[1:]

    limit_arg   = int(_arg(args, "--limit"))   if "--limit"   in args else None
    workers_arg = int(_arg(args, "--workers")) if "--workers" in args else 4
    csv_path    = _arg(args, "--csv")

    _init()
    _migrate()

    if csv_path:
        print("\nExporting no-website leads...")
        export_no_website_csv(csv_path)

    enrich_all(limit=limit_arg, workers=workers_arg)
