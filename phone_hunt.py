"""
Phone hunting ladder — finds phone numbers for businesses in the DB.

Step 1 — Serper Places re-query (name + city).  Logs raw JSON on --log-raw.
Step 2 — Website HTML: tel: links > itemprop=telephone > PHONE_RE scan.
          Checks homepage + /contact + /contact-us + /about in one fetch loop.
Step 3 — Social media pages: extract links from website HTML, try Facebook
          public page (5 s timeout).  Saves all social URLs in social_links
          column so they can be checked manually if scraping is blocked.
Step 4 — Serper web search: '"name" city phone' — scan top-3 snippets.

Every number is validated: normalised to E.164 (+digits), 9–15 total digits,
not all-same-digit, not a premium-rate prefix.

Usage:
    python phone_hunt.py                   # hunt all phone=NULL leads
    python phone_hunt.py --limit 20        # cap at N businesses
    python phone_hunt.py --log-raw         # dump raw Places JSON and exit
    python phone_hunt.py --workers 6       # parallelism (default 4)
"""

import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from database import _init, _conn

PLACES_URL = "https://google.serper.dev/places"
SEARCH_URL  = "https://google.serper.dev/search"


# ── Phone normalization and validation ────────────────────────────────────────

# country code (from _infer_country) → ITU E.164 dial prefix (digits only)
_DIAL: dict[str, str] = {
    "uk": "44", "us": "1", "it": "39",
    "ie": "353", "au": "61", "ca": "1",
}

# E.164 digit-string prefixes that flag premium/non-geographic numbers
_PREMIUM: dict[str, list[str]] = {
    "uk": ["4490", "44870", "44871", "44872", "44873", "44118"],
    "it": ["39899", "39166"],
    "us": ["1900"],
}

# Loose phone pattern — same as website_auditor.PHONE_RE
PHONE_RE = re.compile(
    r"(?<!\w)\+?(?:\(?\d[\d\s\-\.\(\)]{6,17}\d)(?!\w)", re.ASCII
)


def _to_e164(raw: str, country: str | None) -> str | None:
    """
    Normalise raw phone string to E.164 (+digits).
    Returns None if invalid, too short/long, all-same-digit, or premium-rate.

    Handles:
      local trunk-0  (07xxx → +447xxx for UK)
      IDD prefix     (00441234 → +441234)
      already E.164  (+447xxx)
      no trunk-0     (IT mobiles: 349xxx → +39349xxx)
    """
    digits = re.sub(r"\D", "", raw)
    if not (7 <= len(digits) <= 16):
        return None
    if re.match(r"^(.)\1+$", digits):          # 000000000, 111111111 …
        return None

    has_plus = raw.strip().startswith("+")
    dial     = _DIAL.get(country or "", "")

    if has_plus:
        e164 = digits                            # trust the + the caller provided
    elif digits.startswith("00") and dial and digits[2:].startswith(dial):
        e164 = digits[2:]                        # 00441234 → 441234
    elif dial and digits.startswith(dial) and len(digits) > len(dial) + 5:
        e164 = digits                            # already prefixed, no +
    elif dial and digits.startswith("0") and not digits.startswith("00"):
        e164 = dial + digits[1:]                 # 07911… → 447911…
    elif dial and not digits.startswith("0"):
        e164 = dial + digits                     # IT mobile 349… → 39349…
    elif not dial:
        e164 = digits                            # unknown country, accept on length
    else:
        return None

    if not (9 <= len(e164) <= 15):
        return None

    for prefix in _PREMIUM.get(country or "", []):
        if e164.startswith(prefix):
            return None

    return "+" + e164


def _wa_link(e164: str) -> str:
    return "https://wa.me/" + e164.lstrip("+")


# ── Country inference (city stored without suffix after normalisation) ─────────

_UK_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d{1,2}\s\d[A-Z]{2}\b", re.I)
_IT_ADDR_KW     = ("via ", "piazza ", "corso ", "viale ", "vicolo ", " rm,", "roma rm", " italy")
_UK_CITIES      = {
    "manchester", "london", "birmingham", "leeds", "glasgow",
    "liverpool", "bristol", "edinburgh", "sheffield", "cardiff",
    "nottingham", "leicester", "coventry", "bradford",
}
_IT_CITIES = {
    "rome", "roma", "milan", "milano", "florence", "firenze",
    "naples", "napoli", "turin", "torino", "venice", "venezia",
    "bologna", "genoa", "genova",
}


def _infer_country(biz: dict) -> str | None:
    """Best-effort country from domain TLD, address pattern, or city name."""
    domain  = (biz.get("domain") or "").lower()
    address = (biz.get("address") or "").lower()
    city    = (biz.get("city") or "").lower().split(",")[0].strip()

    if any(domain.endswith(t) for t in (".co.uk", ".org.uk", ".me.uk", ".uk")):
        return "uk"
    if domain.endswith(".it"):
        return "it"
    if _UK_POSTCODE_RE.search(address):
        return "uk"
    if any(kw in address for kw in _IT_ADDR_KW):
        return "it"
    if city in _UK_CITIES:
        return "uk"
    if city in _IT_CITIES:
        return "it"
    return None


# ── HTTP helpers ──────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
_TIMEOUT        = (5, 12)
_SOCIAL_TIMEOUT = (4, 5)   # short — Facebook/Instagram block quickly

_tl = threading.local()


def _session() -> requests.Session:
    if not hasattr(_tl, "s"):
        s = requests.Session()
        s.headers.update(_HEADERS)
        s.max_redirects = 5
        _tl.s = s
    return _tl.s


def _fetch(url: str, timeout=_TIMEOUT) -> str | None:
    """GET url; return HTML text or None on any error / non-HTML."""
    try:
        r = _session().get(url, timeout=timeout, allow_redirects=True, verify=False)
        if r.status_code < 400 and "text/html" in r.headers.get("content-type", ""):
            return r.text
    except Exception:
        pass
    return None


# ── Phone extraction from HTML ────────────────────────────────────────────────

def _phone_from_html(html: str) -> str:
    """
    Extract the most-likely phone string from page HTML.
    Priority: tel: href > itemprop=telephone > PHONE_RE on visible text.
    Returns raw string (not yet validated/normalised).
    """
    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if href.lower().startswith("tel:"):
            num = href[4:].strip()
            if len(re.sub(r"\D", "", num)) >= 7:
                return num

    tel_el = soup.find(attrs={"itemprop": "telephone"})
    if tel_el:
        t = tel_el.get_text(strip=True)
        if len(re.sub(r"\D", "", t)) >= 7:
            return t

    for unwanted in soup(["script", "style", "noscript"]):
        unwanted.decompose()
    for m in PHONE_RE.finditer(soup.get_text()):
        digits = re.sub(r"\D", "", m.group())
        if 7 <= len(digits) <= 15:
            return m.group().strip()

    return ""


# ── Social link extraction ────────────────────────────────────────────────────

_SOCIAL_RE = re.compile(
    r"https?://(?:www\.)?(?:facebook|instagram|twitter|x|linkedin|tiktok|youtube)"
    r"\.com/[a-zA-Z0-9_@./%-]{2,80}",
    re.I,
)
# Facebook-only, excluding utility paths that are not profile pages
_FB_PROFILE_RE = re.compile(
    r"https?://(?:www\.)?facebook\.com/"
    r"(?!sharer|share\b|dialog|plugins|login|photo|video|events|groups|pages/create)"
    r"[a-zA-Z0-9_.\-@/%]{2,80}",
    re.I,
)


def _social_urls_from_html(html: str) -> list[str]:
    """Extract all social media profile hrefs from the page (deduped, max 10)."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if _SOCIAL_RE.match(href) and href not in seen:
            seen.add(href)
            out.append(href)
            if len(out) >= 10:
                break
    return out


# ── Ladder steps ──────────────────────────────────────────────────────────────

def _step1_places(name: str, city: str, api_key: str) -> str:
    """Query Serper Places for '{name} in {city}', look for phoneNumber field."""
    try:
        r = _session().post(
            PLACES_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": f"{name} in {city}", "num": 5},
            timeout=(5, 15),
        )
        r.raise_for_status()
        for p in r.json().get("places", [])[:3]:
            title = (p.get("title") or "").lower()
            # Accept if first 8 chars of our name appear in the result title
            if name.lower()[:8] in title:
                phone = (p.get("phoneNumber") or "").strip()
                if phone:
                    return phone
    except Exception:
        pass
    return ""


def _step4_search(name: str, city: str, api_key: str) -> str:
    """Serper web search '"name" city phone' — scan top-3 snippet + title."""
    try:
        r = _session().post(
            SEARCH_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": f'"{name}" {city} phone', "num": 5},
            timeout=(5, 15),
        )
        r.raise_for_status()
        for result in r.json().get("organic", [])[:3]:
            text = (result.get("snippet") or "") + " " + (result.get("title") or "")
            m = PHONE_RE.search(text)
            if m:
                digits = re.sub(r"\D", "", m.group())
                if 7 <= len(digits) <= 15:
                    return m.group().strip()
    except Exception:
        pass
    return ""


# ── Full ladder for one business ──────────────────────────────────────────────

def _hunt_one(
    biz: dict, country: str | None, serper_key: str
) -> tuple[str | None, str, list[str]]:
    """
    Run the 4-step ladder.
    Returns (validated_e164_or_None, source_label, social_link_list).
    """
    name    = biz.get("name") or ""
    city    = biz.get("city") or ""
    website = biz.get("website") or ""

    def validated(raw: str) -> str | None:
        return _to_e164(raw, country) if raw else None

    # ── Step 1 — Serper Places ────────────────────────────────────────────────
    if serper_key:
        e164 = validated(_step1_places(name, city, serper_key))
        if e164:
            return e164, "places", []

    # ── Steps 2 & 3 — website + social (single HTML fetch per page) ───────────
    social_links: list[str] = []

    if website:
        parsed = urlparse(website)
        base   = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

        for path in ("", "/contact", "/contact-us", "/about"):
            html = _fetch(base + path)
            if not html:
                continue

            # Collect social links from homepage only (path == "")
            if path == "":
                social_links = _social_urls_from_html(html)

            # Step 2: phone from this page's HTML
            e164 = validated(_phone_from_html(html))
            if e164:
                return e164, "website", social_links

        # Step 3: try Facebook pages extracted from homepage
        fb_urls = [u for u in social_links if "facebook.com" in u.lower()]
        for fb_url in fb_urls[:2]:
            try:
                r = _session().get(
                    fb_url, timeout=_SOCIAL_TIMEOUT,
                    allow_redirects=True, verify=False,
                )
                # Skip if we landed on a login page
                if r.status_code >= 400 or "login" in r.url.lower():
                    continue
                e164 = validated(_phone_from_html(r.text))
                if e164:
                    return e164, "social", social_links
            except Exception:
                pass  # timeout / block — expected, fall through

    # ── Step 4 — Serper web search ────────────────────────────────────────────
    if serper_key:
        e164 = validated(_step4_search(name, city, serper_key))
        if e164:
            return e164, "search", social_links

    return None, "", social_links


# ── DB helpers ────────────────────────────────────────────────────────────────

def _migrate() -> None:
    """Add phone_source / social_links columns (idempotent)."""
    with _conn() as c:
        for stmt in (
            "ALTER TABLE seen_businesses ADD COLUMN phone_source TEXT",
            "ALTER TABLE seen_businesses ADD COLUMN social_links TEXT",
        ):
            try:
                c.execute(stmt)
            except Exception:
                pass


def _get_targets(limit: int | None = None) -> list[dict]:
    """All businesses that still need a phone number, highest-priority first."""
    sql = (
        "SELECT id, name, website, domain, city, address, no_website, status, pain_score "
        "FROM seen_businesses "
        "WHERE (phone IS NULL OR phone = '') "
        "  AND status NOT IN ('disqualified', 'excluded') "
        "ORDER BY no_website DESC, pain_score DESC NULLS LAST"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
    with _conn() as c:
        return [dict(r) for r in c.execute(sql).fetchall()]


def _save_phone(biz_id: int, phone: str, source: str) -> None:
    try:
        with _conn() as c:
            c.execute(
                "UPDATE seen_businesses SET phone = ?, phone_source = ? WHERE id = ?",
                (phone, source, biz_id),
            )
    except Exception:
        pass  # UNIQUE constraint clash — another row already has this phone


def _save_social_links(biz_id: int, links: list[str]) -> None:
    if not links:
        return
    with _conn() as c:
        c.execute(
            "UPDATE seen_businesses SET social_links = ? WHERE id = ?",
            (json.dumps(links), biz_id),
        )


# ── Raw-log helper (--log-raw) ────────────────────────────────────────────────

def log_raw_places(serper_key: str) -> None:
    """
    Query Serper Places for the first business in the DB and dump the full
    raw JSON so the phone field name can be confirmed.
    """
    _init()
    with _conn() as c:
        row = c.execute("SELECT name, city FROM seen_businesses LIMIT 1").fetchone()
    if not row:
        print("No businesses in DB.")
        return

    name = row["name"]
    city = row["city"]
    print(f"\nLogging raw Serper Places response for: {name!r} in {city!r}\n")

    try:
        r = requests.post(
            PLACES_URL,
            headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
            json={"q": f"{name} in {city}", "num": 5},
            timeout=(5, 20),
        )
        data = r.json()
    except Exception as exc:
        print(f"API error: {exc}")
        return

    places = data.get("places", [])
    if not places:
        print("No places returned.")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    print("First place record (full raw JSON):")
    print(json.dumps(places[0], indent=2, ensure_ascii=False))
    print(f"\n({len(places)} total results)")
    print(f"Fields present : {sorted(places[0].keys())}")
    populated_phones = [p.get("phoneNumber") for p in places if p.get("phoneNumber")]
    print(f"phoneNumber populated in any result: {bool(populated_phones)}")
    if populated_phones:
        print(f"  Values: {populated_phones}")


# ── Main hunt loop ────────────────────────────────────────────────────────────

def hunt_all(limit: int | None = None, workers: int = 4, serper_key: str = "") -> None:
    _init()
    _migrate()

    targets = _get_targets(limit)
    total   = len(targets)
    if total == 0:
        print("No businesses with phone=NULL found.")
        return

    print(f"\nPhone hunt — {total} businesses, {workers} workers")
    print(f"\n{'#':<5} {'Business':<40} {'Phone':<20} {'Src':<8} wa.me")
    print("─" * 98)

    found_count = 0
    miss_count  = 0
    by_source: dict[str, int] = {}
    wa_samples: list[tuple[str, str, str]] = []
    lock    = threading.Lock()
    counter = [0]

    def process(biz: dict) -> None:
        nonlocal found_count, miss_count
        name    = biz.get("name") or "?"
        country = _infer_country(biz)

        phone, source, slinks = _hunt_one(biz, country, serper_key)

        with lock:
            counter[0] += 1
            n = counter[0]
            _save_social_links(biz["id"], slinks)

            if phone:
                _save_phone(biz["id"], phone, source)
                found_count += 1
                by_source[source] = by_source.get(source, 0) + 1
                wa = _wa_link(phone)
                wa_samples.append((name, phone, wa))
                print(f"{n:<5} {name[:39]:<40} {phone:<20} {source:<8} {wa}")
            else:
                miss_count += 1
                social_note = f"  ({len(slinks)} social URLs saved)" if slinks else ""
                print(f"{n:<5} {name[:39]:<40} {'—':<20} {'':8}{social_note}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(process, b) for b in targets]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                with lock:
                    print(f"  [error] {exc}")

    print("─" * 98)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print("BACKFILL RESULTS")
    print(f"{'='*55}")
    print(f"  Processed           : {total}")
    print(f"  Phones found        : {found_count}")
    print(f"  Not found           : {miss_count}")
    if by_source:
        print(f"\n  By source:")
        for src in ("places", "website", "social", "search"):
            n = by_source.get(src, 0)
            if n:
                bar = "█" * n
                print(f"    {src:<10} {n:>3}  {bar}")

    with _conn() as c:
        nw_total = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses "
            "WHERE no_website=1 AND status='qualified'"
        ).fetchone()["n"]
        nw_phone = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses "
            "WHERE no_website=1 AND status='qualified' "
            "AND phone IS NOT NULL AND phone != ''"
        ).fetchone()["n"]
        all_phone = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses "
            "WHERE phone IS NOT NULL AND phone != ''"
        ).fetchone()["n"]

    print(f"\n  No-website qualified leads  : {nw_phone} / {nw_total} have phones")
    print(f"  All leads with phone in DB  : {all_phone}")

    if wa_samples:
        print(f"\n  Sample wa.me links:")
        for name, phone, wa in wa_samples[:10]:
            print(f"    {name[:35]:<36} {phone:<18}  {wa}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _arg(args: list[str], flag: str, default=None):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return default


if __name__ == "__main__":
    args       = sys.argv[1:]
    serper_key = os.getenv("SERPER_API_KEY", "")

    if not serper_key:
        print("WARNING: SERPER_API_KEY not set — Steps 1 and 4 will be skipped.")

    if "--log-raw" in args:
        log_raw_places(serper_key)
        sys.exit(0)

    limit_arg   = int(_arg(args, "--limit"))   if "--limit"   in args else None
    workers_arg = int(_arg(args, "--workers")) if "--workers" in args else 4

    hunt_all(limit=limit_arg, workers=workers_arg, serper_key=serper_key)
