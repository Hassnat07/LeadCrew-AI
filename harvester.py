"""
Stage 1 — Harvester (v2 — robust, parallel, save-as-you-go)

Serper Places field names (confirmed live 2026-06-12):
    title, address, latitude, longitude, rating, ratingCount, category,
    phoneNumber, website, cid
    (phoneNumber is local format e.g. '07928 486113' — normalised to E.164 on save)

Website resolution — "Places first, search as fallback" model:
    1. Use the website field from the Places response directly
       (website_source='places').  Google's own listing is highest trust.
       - Social domain (facebook.com, instagram.com, …) → stored in
         social_links column, no_website=True.
       - Directory-blocked domain → no_website=True (no further search).
    2. Only when Places returns no website: fall back to Serper Search with
       "reject unless proven" rules:
         PROOF 1 — Domain-name match: a distinctive name token (>=4 chars,
                   after stripping stop words) appears verbatim in the domain.
         PROOF 2 — Phone match: business phone (last 9 digits) found in HTML.
       Hard pre-filters on fallback candidates:
         - Domain not in DIRECTORY_BLOCKLIST / path fragments / staging prefix
         - URL path depth <= 1
         - Domain not an exclusive token of a DIFFERENT batch business
           (cross-contamination guard)
    If no website found → no_website=True (route to phone/WhatsApp campaign).
"""
import json
import os
import re
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PLACES_URL = "https://google.serper.dev/places"
SEARCH_URL  = "https://google.serper.dev/search"

# Domains that can never be a business's own website.
DIRECTORY_BLOCKLIST: frozenset[str] = frozenset({
    # Government / registry
    "company-information.service.gov.uk",
    "find-and-update.company-information.service.gov.uk",
    # Trade directories
    "mybuilder.com", "checkatrade.com", "trustatrader.com", "ratedpeople.com",
    "bark.com", "yell.com", "yell.co.uk", "tradesupermarket.com",
    "tradesupermarket.co.uk", "buildersup.co.uk", "threebestrated.com",
    "threebestrated.co.uk", "thomsonlocal.com", "cylex-uk.co.uk", "cylex.us",
    "freeindex.co.uk", "scoot.co.uk", "hotfrog.co.uk", "find-open.co.uk",
    "opendi.co.uk", "plumbers-directory.co.uk",
    "wheree.com", "which.co.uk", "northdata.de",
    # Review platforms
    "birdeye.com", "reviews.birdeye.com", "trustpilot.com",
    "yelp.com", "yelp.co.uk", "tripadvisor.com", "tripadvisor.co.uk",
    # Social / aggregator
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    # Booking / health
    "zocdoc.com", "healthgrades.com", "ratemds.com", "appointmentplus.com",
    "nhs.uk",
    # Map / local
    "foursquare.com", "nextdoor.com", "google.com",
    "yellowpages.com", "businesslist.us", "bbb.org",
    # Home / property
    "houzz.com", "houzz.co.uk", "angi.com", "thumbtack.com",
    # Hosted blog / CMS platforms — matches subdomains: *.blogspot.com, *.wordpress.com
    "blogspot.com", "wordpress.com",
})

# Social platforms — if a GMB listing points here we store the URL in
# social_links and treat the business as no_website for audit purposes.
_SOCIAL_DOMAINS: frozenset[str] = frozenset({
    "facebook.com", "instagram.com", "linkedin.com",
    "twitter.com", "x.com", "tiktok.com", "youtube.com",
})

_BLOCKLIST_PATH_FRAGMENTS: frozenset[str] = frozenset({
    "/directory/", "/profile/", "/listing/", "/company/", "/reviews/",
})

_STAGING_PREFIXES = ("staging", "dev", "test")


def _is_directory_url(url: str) -> bool:
    if not url:
        return True
    domain = _extract_domain(url)
    if not domain:
        return True
    for blocked in DIRECTORY_BLOCKLIST:
        if domain == blocked or domain.endswith("." + blocked):
            return True
    # Dynamic pattern: any *-directory.co.uk or *-directory.com
    if re.search(r"-directory\.(co\.uk|com)$", domain):
        return True
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        path = parsed.path.lower()
        if any(frag in path for frag in _BLOCKLIST_PATH_FRAGMENTS):
            return True
        netloc = parsed.netloc.lower()
        if "." in netloc:
            sub = netloc.split(".")[0]
            if any(sub == p or sub.startswith(p + "-") for p in _STAGING_PREFIXES):
                return True
    except Exception:
        pass
    return False


def _is_social_domain(domain: str) -> bool:
    """True if domain is a social-media platform (not a real business website)."""
    return any(domain == s or domain.endswith("." + s) for s in _SOCIAL_DOMAINS)


def _is_path_shallow(url: str) -> bool:
    """True if URL path has at most 1 non-empty segment (homepage or /page-name)."""
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        segments = [s for s in parsed.path.strip("/").split("/") if s]
        return len(segments) <= 1
    except Exception:
        return True  # parse failure — don't block on it


def _name_tokens(name: str) -> list[str]:
    """Distinctive words (>=4 chars) from a business name, minus legal/stop words."""
    cleaned = re.sub(
        r"\b(ltd|limited|inc|llc|plc|co\.?|and|the|of|for|&|services|service)\b",
        "", name.lower(),
    )
    return [w for w in re.sub(r"[^\w\s]", "", cleaned).split() if len(w) >= 4]


def _domain_matches_name(domain: str, name: str) -> bool:
    tokens = _name_tokens(name)
    if not tokens:
        return False
    domain_flat = re.sub(r"[.\-]", "", domain.lower())
    return any(t in domain_flat for t in tokens)


def _normalize_phone(phone: str) -> str:
    """Last 9 digits of a phone number — enough to match across country codes."""
    digits = re.sub(r"\D", "", phone)
    return digits[-9:] if len(digits) >= 9 else digits


# ── Thread-local HTTP sessions ────────────────────────────────────────────────

_thread_local = threading.local()


def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=2, backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = _make_session()
    return _thread_local.session


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_domain(url: str) -> str | None:
    if not url:
        return None
    try:
        if "://" not in url:
            url = "https://" + url
        host = urlparse(url).netloc.lower()
        return re.sub(r"^www\.", "", host) or None
    except Exception:
        return None


_US_STATE_ZIP_RE = re.compile(r",\s*[A-Z]{2}\s+\d{5}(-\d{4})?")

# Matches SLDs like "manchesterfamilydentistrynh" (ends with a US state abbrev,
# with at least 2 chars of prefix so bare state names are excluded).
_US_STATE_SLD_RE = re.compile(
    r".{2,}(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md"
    r"|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc"
    r"|sd|tn|tx|ut|vt|va|wa|wv|wi|wy)$"
)

_COUNTRY_HINTS: dict[str, str] = {
    "uk": "uk", "united kingdom": "uk", "england": "uk", "gb": "uk",
    "us": "us", "usa": "us", "united states": "us", "america": "us",
    "canada": "ca", "ca": "ca",
    "australia": "au", "au": "au",
    "ireland": "ie", "ie": "ie",
    "germany": "de", "de": "de", "deutschland": "de",
    "france": "fr", "fr": "fr",
    "spain": "es", "es": "es",
    "italy": "it", "it": "it", "italia": "it",
    "netherlands": "nl", "nl": "nl", "holland": "nl",
}

# Local-language names for cities whose English name differs from the native form.
# Key: English city name (lower); value: list of native spellings (lower).
_CITY_ALIASES: dict[str, list[str]] = {
    "rome":      ["roma"],
    "milan":     ["milano"],
    "florence":  ["firenze"],
    "naples":    ["napoli"],
    "turin":     ["torino"],
    "munich":    ["münchen", "munchen"],
    "vienna":    ["wien"],
    "prague":    ["praha"],
    "lisbon":    ["lisboa"],
    "warsaw":    ["warszawa"],
}


def _parse_target_country(city: str) -> str | None:
    parts = city.split(",")
    if len(parts) < 2:
        return None
    return _COUNTRY_HINTS.get(parts[-1].strip().lower())


def _normalize_city_for_db(city: str) -> str:
    """Strip country suffix for dedup/storage: 'Manchester, UK' → 'Manchester'."""
    return city.split(",")[0].strip()


def _suspected_us_domain(domain: str, city: str) -> bool:
    """True when a .com domain's SLD ends with a US state abbrev and city targets UK."""
    if _parse_target_country(city) != "uk":
        return False
    parts = domain.lower().split(".")
    if parts[-1] != "com":
        return False
    sld = parts[0]
    return bool(_US_STATE_SLD_RE.match(sld))


def _city_in_address(address: str, city: str) -> bool:
    if not address or not city:
        return True
    addr_lower = address.lower()
    city_main  = city.strip().split(",")[0].strip().lower()
    # Accept city name OR any known local-language alias (Rome → Roma, etc.)
    candidates = {city_main} | set(_CITY_ALIASES.get(city_main, []))
    if not any(c in addr_lower for c in candidates):
        return False
    target_country = _parse_target_country(city)
    if target_country != "us" and _US_STATE_ZIP_RE.search(address):
        return False
    return True


# ── Serper API calls ──────────────────────────────────────────────────────────

def _get_places(niche: str, city: str, api_key: str) -> list[dict]:
    all_places: list[dict] = []
    seen_cids: set[str] = set()
    session = _get_session()

    for page in range(1, 5):
        try:
            resp = session.post(
                PLACES_URL,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": f"{niche} in {city}", "num": 100, "page": page},
                timeout=(5, 20),
            )
            resp.raise_for_status()
            places = resp.json().get("places", [])
            if not places:
                break
            new_count = 0
            for p in places:
                cid = p.get("cid", "")
                if cid:
                    if cid in seen_cids:
                        continue
                    seen_cids.add(cid)
                if not _city_in_address(p.get("address", ""), city):
                    continue
                all_places.append(p)
                new_count += 1
            if new_count == 0:
                break
        except Exception as exc:
            print(f"  [Places page {page}] {exc}")
            break

    return all_places


def _find_website(
    name: str,
    city: str,
    api_key: str,
    phone: str = "",
    blocking_tokens: frozenset = frozenset(),
) -> tuple[str, str]:
    """
    Returns (url, source) where source is "domain_match" | "phone_match" | "".

    Candidate URLs must pass all hard pre-filters, then at least one proof:
      PROOF 1 — Domain-name match (no page fetch needed, fastest)
      PROOF 2 — Phone in page HTML (requires phone; currently unavailable from Places)
    """
    try:
        resp = _get_session().post(
            SEARCH_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": f'"{name}" {city}', "num": 5},
            timeout=(5, 15),
        )
        if resp.status_code != 200:
            return "", ""

        candidates: list[tuple[str, str]] = []  # (url, domain)
        for r in resp.json().get("organic", []):
            url = r.get("link", "")
            if not url or _is_directory_url(url):
                continue
            if not _is_path_shallow(url):
                continue
            domain = _extract_domain(url)
            if not domain:
                continue
            # Cross-business guard: reject if domain matches an exclusive token
            # that belongs to a DIFFERENT business in this harvest batch.
            if blocking_tokens:
                domain_flat = re.sub(r"[.\-]", "", domain.lower())
                if any(t in domain_flat for t in blocking_tokens):
                    continue
            candidates.append((url, domain))

        # PROOF 1 — domain-name match (no HTTP fetch)
        for url, domain in candidates:
            if _domain_matches_name(domain, name):
                return url, "domain_match"

        # PROOF 2 — phone number present in page HTML
        if phone:
            target = _normalize_phone(phone)
            if len(target) >= 7:
                for url, _ in candidates:
                    try:
                        r2 = _get_session().get(
                            url, timeout=(5, 12), allow_redirects=True,
                            verify=False, stream=True,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; LeadBot/2.0)"},
                        )
                        if r2.status_code >= 400:
                            r2.close()
                            continue
                        buf = b""
                        for chunk in r2.iter_content(4096):
                            buf += chunk
                            if len(buf) >= 50_000:
                                break
                        r2.close()
                        text = buf.decode("utf-8", errors="ignore")
                        page_phones = {
                            re.sub(r"\D", "", m)[-9:]
                            for m in re.findall(r"[\d\s()\-+.]{7,20}", text)
                            if len(re.sub(r"\D", "", m)) >= 7
                        }
                        if target in page_phones:
                            return url, "phone_match"
                    except Exception:
                        continue

        return "", ""
    except Exception:
        pass
    return "", ""


def _lookup_one(
    place: dict,
    city: str,
    api_key: str,
    blocking_tokens: frozenset = frozenset(),
) -> dict:
    """
    Enrich one place dict with website/domain/source/phone.  Never raises.

    Website resolution order:
      1. Places website field (highest trust — Google's own listing).
         Real site → website_source='places', skip secondary search.
         Social domain → social_links column, no_website=True.
         Directory blocked → no_website=True.
      2. _find_website() — ONLY when Places returns no website field at all.
    """
    name           = (place.get("title") or "").strip()
    raw_phone      = (place.get("phoneNumber") or "").strip()
    places_website = (place.get("website") or "").strip()

    website        = ""
    domain         = None
    website_source = None
    social_links   = None

    # ── Phone: normalize Places phoneNumber to E.164 ──────────────────────────
    phone = None
    if raw_phone:
        try:
            from phone_hunt import _to_e164
            country = _parse_target_country(city)
            phone   = _to_e164(raw_phone, country)
        except Exception:
            phone = raw_phone or None

    # ── Website: Places field takes full priority ──────────────────────────────
    if places_website:
        _dom = _extract_domain(places_website)
        if _dom:
            if _is_social_domain(_dom):
                # GMB listing points at a social profile — save the URL but
                # treat as no_website for audit purposes.
                social_links = json.dumps([places_website])
            elif not _is_directory_url(places_website):
                # Clean real website — use it directly, no secondary search.
                website        = places_website
                website_source = "places"
                domain         = _dom
        # If Places gave us any website (social / blocked / bad domain),
        # respect it — do NOT fall back to _find_website().
    else:
        # Places has no website at all → secondary search as fallback.
        try:
            website, website_source = _find_website(
                name, city, api_key,
                phone=raw_phone, blocking_tokens=blocking_tokens,
            )
            domain = _extract_domain(website) if website else None
            if not domain:
                website        = ""
                website_source = None
        except Exception as exc:
            print(f"  [Lookup] {name}: {exc}")

    place["website"]        = website or None
    place["domain"]         = domain
    place["no_website"]     = domain is None
    place["website_source"] = website_source
    place["phone"]          = phone
    place["social_links"]   = social_links
    return place


# ── Core harvest function ─────────────────────────────────────────────────────

def harvest(niche: str, city: str, api_key: str) -> list[dict]:
    """
    Harvest one (niche, city) pair:
      1. Fetch places from Serper (multi-page, geo-filtered).
      2. Build exclusive-token map for cross-business contamination guard.
      3. Look up each website in parallel (max 5 workers).
      4. Accept website only if domain-name match OR phone match is proven.
      5. Dedup by domain (within-run set + DB check).
      6. Save to DB immediately — interrupted runs keep progress.
    """
    from database import add_business, domain_seen, name_city_seen

    raw = _get_places(niche, city, api_key)
    total = len(raw)
    print(f"  Serper Places: {total} businesses (after geo-filter).")
    if total == 0:
        return []

    # Exclusive-token map: token -> business_name, for tokens that appear in
    # exactly ONE business name in this batch (generic tokens excluded).
    token_counts: Counter = Counter(
        t
        for p in raw
        for t in _name_tokens((p.get("title") or "").strip())
    )
    exclusive_tokens: dict[str, str] = {}
    for p in raw:
        biz_name = (p.get("title") or "").strip()
        for t in _name_tokens(biz_name):
            if token_counts[t] == 1:
                exclusive_tokens[t] = biz_name

    added: list[dict] = []
    seen_this_run: set[str] = set()
    counter = [0]
    lock = threading.Lock()

    def process(place: dict) -> dict | None:
        name = (place.get("title") or "").strip()
        if not name:
            return None

        # Tokens that exclusively identify a DIFFERENT business in this batch
        blocking = frozenset(
            t for t, owner in exclusive_tokens.items() if owner != name
        )

        enriched  = _lookup_one(place, city, api_key, blocking_tokens=blocking)
        domain    = enriched["domain"]
        city_norm = _normalize_city_for_db(city)  # strip ", UK" / ", Italy" etc.

        with lock:
            counter[0] += 1
            label = f"{counter[0]}/{total}"

            address_raw = (place.get("address") or "").strip()
            if domain and not address_raw and _suspected_us_domain(domain, city):
                print(f"  [{label}] {name} ({domain}) -> suspected US, skipped")
                return None

            if domain:
                if domain in seen_this_run:
                    print(f"  [{label}] {name} -> skip (dup domain in run)")
                    return None
                seen_this_run.add(domain)
                if domain_seen(domain):
                    print(f"  [{label}] {name} -> skip (already in DB: {domain})")
                    return None
            else:
                if name_city_seen(name, city_norm):
                    print(f"  [{label}] {name} -> skip (name+city already in DB)")
                    return None

            biz = {
                "name":           name,
                "domain":         domain,
                "phone":          enriched.get("phone"),
                "niche":          niche,
                "city":           city_norm,  # normalized — no country suffix
                "rating":         place.get("rating"),
                "rating_count":   place.get("ratingCount"),
                "pain_score":     None,
                "issues_json":    None,
                "status":         "harvested",
                "audited_at":     None,
                "website":        enriched["website"],
                "address":        (place.get("address") or "").strip(),
                "category":       (place.get("category") or "").strip(),
                "no_website":     1 if enriched["no_website"] else 0,
                "website_source": enriched["website_source"],
                "social_links":   enriched.get("social_links"),
            }

            inserted = add_business(biz)
            if inserted:
                src = enriched["website_source"] or ""
                tag = "NO WEBSITE" if enriched["no_website"] else f"{enriched['website']} [{src}]"
                print(f"  [{label}] {name} -> SAVED ({tag})")
                return biz
            else:
                print(f"  [{label}] {name} -> DB conflict, skipped")
                return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(process, p): p for p in raw}
        for fut in as_completed(futures):
            try:
                result = fut.result()
                if result:
                    added.append(result)
            except Exception as exc:
                name = futures[fut].get("title", "?")
                print(f"  [Worker error] {name}: {exc}")

    return added


def expand_niche_synonyms(niche: str, openai_api_key: str, n: int = 3) -> list[str]:
    """
    Ask the LLM for n alternate phrasings of a business niche, used to widen a
    Places search when one query's pool of new leads is exhausted by dedup
    (e.g. "car dealership" -> "auto dealer", "used car dealer"). Returns []
    on any failure — caller just keeps the original query in that case.
    """
    try:
        from openai import OpenAI
        client = OpenAI(api_key=openai_api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f'Give me {n} alternate search phrasings someone would type into '
                    f'Google Maps for the business category "{niche}". Same category, '
                    f'different wording a directory or Google listing might use. '
                    f'Respond with JSON: {{"synonyms": ["...", "..."]}}'
                ),
            }],
            response_format={"type": "json_object"},
            temperature=0.5,
            max_tokens=150,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        syns = [s.strip() for s in data.get("synonyms", []) if isinstance(s, str) and s.strip()]
        return syns[:n]
    except Exception:
        return []


def harvest_batch(pairs: list[tuple[str, str]], api_key: str) -> list[dict]:
    """Harvest multiple (niche, city) pairs and return all newly added businesses."""
    all_added: list[dict] = []
    for niche, city in pairs:
        print(f"\n{'='*60}")
        print(f"Harvesting: {niche} in {city}")
        print(f"{'='*60}")
        added = harvest(niche, city, api_key)
        print(f"\n  -> {len(added)} new businesses added for {niche}/{city}")
        all_added.extend(added)
    return all_added


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    api_key = os.getenv("SERPER_API_KEY", "")
    if not api_key:
        print("ERROR: SERPER_API_KEY not found in .env")
        sys.exit(1)

    args = sys.argv[1:]
    reset = "--reset" in args
    args  = [a for a in args if a != "--reset"]

    if len(args) < 2:
        print("Usage:   python harvester.py [--reset] \"niche\" \"city\"")
        print("Example: python harvester.py --reset \"plumbers\" \"Manchester, UK\"")
        sys.exit(1)

    niche = args[0]
    city  = args[1]

    if reset:
        from database import reset_city
        deleted = reset_city(city)
        print(f"  Cleared {deleted} existing records for city='{city}'")

    print(f"\nHarvesting: {niche} in {city}")
    results = harvest(niche, city, api_key)

    print(f"\n{'='*60}")
    print(f"NEW BUSINESSES HARVESTED: {len(results)}")
    print(f"{'='*60}")

    for b in results:
        rating_str = (
            f"{b['rating']}* ({b['rating_count']} reviews)"
            if b.get("rating") else "No rating data"
        )
        print(f"\n  {b['name']}")
        print(f"    Address  : {b['address']}")
        print(f"    Website  : {b.get('website') or '(none)'}")
        if b.get("website_source"):
            print(f"    Source   : {b['website_source']}")
        print(f"    Rating   : {rating_str}")
        print(f"    Category : {b['category']}")
        if b["no_website"]:
            print(f"    Status   : NO WEBSITE -- routed to phone/WhatsApp campaign")
        else:
            print(f"    Status   : Has website -- will proceed to audit")
