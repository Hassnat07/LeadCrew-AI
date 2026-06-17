"""
Website audit module.
v1: PageSpeed + HTML scraping (used by agents.py / app.py).
v2: 15-check deep audit returning (triggered, evidence) per check; see audit_site().
"""
import re
import time
import threading
import datetime
import requests
import urllib3
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, urljoin, unquote
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── Signature dictionaries ────────────────────────────────────────────────────

CHATBOT_SIGNATURES: dict[str, str] = {
    "intercom": "Intercom",
    "drift.com": "Drift",
    "crisp.chat": "Crisp",
    "tidio": "Tidio",
    "tawk.to": "Tawk.to",
    "freshchat": "Freshchat",
    "zendesk": "Zendesk Chat",
    "hubspot": "HubSpot Chat",
    "livechat": "LiveChat",
    "olark": "Olark",
    "smartsupp": "Smartsupp",
    "jivochat": "JivoChat",
    "purechat": "Pure Chat",
    "chatra": "Chatra",
    "chatbase": "Chatbase AI",
    "botpress": "Botpress",
    "voiceflow": "Voiceflow",
    "manychat": "ManyChat",
    "chatfuel": "Chatfuel",
    "tidio.com": "Tidio",
    "gorgias": "Gorgias",
}

OUTDATED_SIGNATURES: list[tuple[str, str]] = [
    (r"jquery[/\-]1\.", "jQuery 1.x (very outdated)"),
    (r"jquery[/\-]2\.", "jQuery 2.x (outdated)"),
    (r"bootstrap[/\-]3\.", "Bootstrap 3 (outdated)"),
    (r"bootstrap[/\-]2\.", "Bootstrap 2 (very outdated)"),
    (r"angularjs|angular\.js", "AngularJS 1.x (end-of-life)"),
    (
        r"twenty(?:ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen)/style\.css",
        "Old WordPress default theme",
    ),
]

CTA_KEYWORDS: list[str] = [
    "get started", "book a call", "schedule a call", "contact us",
    "get a quote", "free trial", "sign up", "buy now", "request a demo",
    "get in touch", "start now", "try free", "order now", "book now",
    "speak to us", "talk to us",
]

PAGESPEED_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Matches phone numbers: optional +country-code, area code, then 2 groups of digits
PHONE_RE = re.compile(
    r'(?<!\w)\+?(?:\(?\d[\d\s\-\.\(\)]{6,17}\d)(?!\w)',
    re.ASCII,
)


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class WebsiteAuditResult:
    url: str
    desktop_score: Optional[int] = None
    mobile_score: Optional[int] = None
    fcp_s: Optional[float] = None   # First Contentful Paint (seconds)
    lcp_s: Optional[float] = None   # Largest Contentful Paint (seconds)
    has_chatbot: bool = False
    chatbot_name: str = ""
    has_viewport: bool = True
    outdated_tech: list = field(default_factory=list)
    has_form: bool = False
    has_cta: bool = False
    phone_number: str = ""
    issues: list = field(default_factory=list)
    error: Optional[str] = None

    def priority(self) -> str:
        score = self.mobile_score if self.mobile_score is not None else self.desktop_score
        if score is None:
            return "UNKNOWN"
        if score < 50:
            return "HIGH"
        if score < 80:
            return "MEDIUM"
        return "LOW"

    def score_badge(self) -> str:
        parts = []
        if self.desktop_score is not None:
            parts.append(f"Desktop {self.desktop_score}/100")
        if self.mobile_score is not None:
            parts.append(f"Mobile {self.mobile_score}/100")
        return "  |  ".join(parts) if parts else "Score unavailable"

    def to_agent_summary(self) -> str:
        if self.error and self.desktop_score is None and self.mobile_score is None:
            return (
                f"[AUDIT FAILED for {self.url}]\n"
                f"Error: {self.error}\n"
                "Note to copywriter: website could not be audited. "
                "Use any visible clues from the lead research instead."
            )

        lines = [f"=== WEBSITE AUDIT REPORT: {self.url} ==="]

        if self.desktop_score is not None:
            grade = "POOR" if self.desktop_score < 50 else "NEEDS WORK" if self.desktop_score < 80 else "GOOD"
            lines.append(f"Desktop Performance Score : {self.desktop_score}/100  [{grade}]")

        if self.mobile_score is not None:
            grade = "POOR" if self.mobile_score < 50 else "NEEDS WORK" if self.mobile_score < 80 else "GOOD"
            lines.append(f"Mobile Performance Score  : {self.mobile_score}/100  [{grade}]")

        if self.fcp_s is not None:
            lines.append(f"First Contentful Paint    : {self.fcp_s:.1f}s")
        if self.lcp_s is not None:
            lines.append(f"Largest Contentful Paint  : {self.lcp_s:.1f}s")

        lines.append(
            f"AI Chatbot / Live Chat    : {'YES — ' + self.chatbot_name if self.has_chatbot else 'NOT FOUND'}"
        )
        lines.append(
            f"Mobile Viewport Meta Tag  : {'Present' if self.has_viewport else 'MISSING — site not mobile-ready'}"
        )
        lines.append(
            f"Outdated Technology       : {', '.join(self.outdated_tech) if self.outdated_tech else 'None detected'}"
        )
        lines.append(f"Contact Form              : {'Found' if self.has_form else 'Not found'}")
        lines.append(f"Phone Number              : {self.phone_number if self.phone_number else 'Not found'}")
        lines.append(
            f"Clear CTA Buttons         : {'Found' if self.has_cta else 'NOT FOUND — poor conversion layout'}"
        )
        lines.append(f"Overall Priority          : {self.priority()}")

        if self.issues:
            lines.append(f"\nSpecific Issues Found ({len(self.issues)}):")
            for issue in self.issues:
                lines.append(f"  • {issue}")
        else:
            lines.append("\nNo critical issues detected.")

        lines.append("=== END OF AUDIT ===")
        return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_seconds(display_value: str) -> Optional[float]:
    if not display_value:
        return None
    clean = display_value.replace(",", "").replace(" ", "").strip()
    ms = re.search(r"([\d.]+)\s*ms", clean)
    if ms:
        return float(ms.group(1)) / 1000
    sec = re.search(r"([\d.]+)\s*s", clean)
    if sec:
        return float(sec.group(1))
    return None


def _pagespeed(url: str, strategy: str, api_key: str = "") -> Optional[dict]:
    params: dict = {"url": url, "strategy": strategy}
    if api_key:
        params["key"] = api_key
    try:
        resp = requests.get(PAGESPEED_ENDPOINT, params=params, timeout=45)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _extract_phone(soup: BeautifulSoup) -> str:
    """Extract a phone number from a parsed page, preferring tel: links."""
    # 1. tel: anchor href — most reliable signal
    for a in soup.find_all('a', href=True):
        href = a.get('href', '')
        if href.lower().startswith('tel:'):
            number = href[4:].strip()
            if len(re.sub(r'\D', '', number)) >= 7:
                return number
    # 2. schema.org itemprop="telephone"
    tel_elem = soup.find(attrs={'itemprop': 'telephone'})
    if tel_elem:
        t = tel_elem.get_text(strip=True)
        if len(re.sub(r'\D', '', t)) >= 7:
            return t
    # 3. Regex scan of visible page text
    for m in PHONE_RE.finditer(soup.get_text()):
        digits = re.sub(r'\D', '', m.group())
        if 7 <= len(digits) <= 15:
            return m.group().strip()
    return ""


# ── Public API ────────────────────────────────────────────────────────────────

def audit_website(url: str, pagespeed_api_key: str = "") -> WebsiteAuditResult:
    """
    Full website audit: PageSpeed (desktop + mobile) + HTML scraping.
    Works without an API key (25 free calls/day shared limit).
    Pass a free Google Cloud API key to raise the limit.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    result = WebsiteAuditResult(url=url)

    # ── Desktop PageSpeed ─────────────────────────────────────────────────────
    desktop = _pagespeed(url, "desktop", pagespeed_api_key)
    if desktop:
        try:
            cats = desktop["lighthouseResult"]["categories"]
            audits = desktop["lighthouseResult"]["audits"]
            result.desktop_score = round(cats["performance"]["score"] * 100)
            result.fcp_s = _extract_seconds(
                audits.get("first-contentful-paint", {}).get("displayValue", "")
            )
            result.lcp_s = _extract_seconds(
                audits.get("largest-contentful-paint", {}).get("displayValue", "")
            )
        except (KeyError, TypeError):
            pass

    # ── Mobile PageSpeed ──────────────────────────────────────────────────────
    time.sleep(1)  # Avoid hitting API rate limits
    mobile = _pagespeed(url, "mobile", pagespeed_api_key)
    if mobile:
        try:
            cats = mobile["lighthouseResult"]["categories"]
            audits = mobile["lighthouseResult"]["audits"]
            result.mobile_score = round(cats["performance"]["score"] * 100)
            vp = audits.get("viewport", {})
            result.has_viewport = vp.get("score", 1) == 1
        except (KeyError, TypeError):
            pass

    # ── HTML Scrape ───────────────────────────────────────────────────────────
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"
                )
            },
        )
        html_lower = resp.text.lower()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Chatbot detection
        for sig, name in CHATBOT_SIGNATURES.items():
            if sig in html_lower:
                result.has_chatbot = True
                result.chatbot_name = name
                break

        # Outdated technology
        for pattern, label in OUTDATED_SIGNATURES:
            if re.search(pattern, html_lower) and label not in result.outdated_tech:
                result.outdated_tech.append(label)

        # Contact form (at least 2 inputs inside a <form>)
        for form in soup.find_all("form"):
            if len(form.find_all("input")) >= 2:
                result.has_form = True
                break

        # CTA presence
        page_text = soup.get_text().lower()
        result.has_cta = any(kw in page_text for kw in CTA_KEYWORDS)

        # Phone number from homepage
        result.phone_number = _extract_phone(soup)

    except Exception as exc:
        if result.desktop_score is None and result.mobile_score is None:
            result.error = str(exc)

    # ── Contact-page fallback: try /contact etc. if form or phone still missing ─
    if not result.has_form or not result.phone_number:
        _ua = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            )
        }
        for _path in ('/contact', '/contact-us', '/contactus', '/get-in-touch', '/reach-us'):
            try:
                _cu = url.rstrip('/') + _path
                _cr = requests.get(_cu, timeout=10, headers=_ua)
                if _cr.status_code != 200:
                    continue
                _cs = BeautifulSoup(_cr.text, 'html.parser')
                if not result.has_form:
                    for _f in _cs.find_all('form'):
                        if len(_f.find_all('input')) >= 2:
                            result.has_form = True
                            break
                if not result.phone_number:
                    result.phone_number = _extract_phone(_cs)
                if result.has_form and result.phone_number:
                    break
            except Exception:
                continue

    # ── Compile Issues ────────────────────────────────────────────────────────
    if result.desktop_score is not None:
        if result.desktop_score < 50:
            result.issues.append(
                f"Very poor desktop performance ({result.desktop_score}/100) — site loads extremely slowly"
            )
        elif result.desktop_score < 80:
            result.issues.append(
                f"Below-average desktop speed ({result.desktop_score}/100) — needs optimisation"
            )

    if result.mobile_score is not None:
        if result.mobile_score < 50:
            result.issues.append(
                f"Critical mobile failure ({result.mobile_score}/100) — effectively unusable on phones"
            )
        elif result.mobile_score < 80:
            result.issues.append(
                f"Poor mobile experience ({result.mobile_score}/100) — hurts Google rankings and conversions"
            )

    if result.lcp_s is not None and result.lcp_s > 4.0:
        result.issues.append(
            f"Slow page load — LCP is {result.lcp_s:.1f}s (Google benchmark: < 2.5s)"
        )

    if not result.has_chatbot:
        result.issues.append(
            "No AI chatbot or live chat found — missing 24/7 customer engagement"
        )

    if not result.has_viewport:
        result.issues.append(
            "Missing mobile viewport meta tag — site is not configured for mobile devices"
        )

    if result.outdated_tech:
        result.issues.append(
            f"Outdated technology detected: {', '.join(result.outdated_tech)}"
        )

    if not result.has_form:
        result.issues.append("No contact form found — visitors have no easy way to enquire")

    if not result.has_cta:
        result.issues.append(
            "No clear call-to-action buttons — poor conversion-focused layout"
        )

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# v2  Deep audit  — each check returns (triggered: bool, evidence: str)
# 15 spec checks + legacy chatbot/outdated-tech/contact-form.
# Thread-safe: one requests.Session per thread (reused across checks).
# PageSpeed: cached 30 days in DB; only called when ≥1 cheap HTML issue found.
# ═══════════════════════════════════════════════════════════════════════════════

# Serialize PageSpeed calls: one at a time, minimum gap between requests.
# Without an API key the free shared quota is 25 calls/day — bursting 8 workers
# at once exhausts it immediately. The semaphore + gap prevents 429s.
_pagespeed_sem    = threading.Semaphore(1)
_pagespeed_prev_t: list[float] = [0.0]

_V2_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_v2_local = threading.local()

# ── Constants ─────────────────────────────────────────────────────────────────

_FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "hotmail.co.uk",
    "outlook.com", "live.com", "live.co.uk", "icloud.com", "aol.com", "me.com",
    "googlemail.com",
})

_BOOKING_PATTERNS = (
    "calendly.com", "booksy.com", "fresha.com", "setmore.com",
    "acuityscheduling.com", "youcanbook.me", "appointy.com",
    "vagaro.com", "mindbody", "timely.cloud", "squareup.com/appointments",
    "schedulicity.com", "treatwell.co.uk", "treatwell.com",
    "book online", "book an appointment", "schedule online",
    "book a consultation", "book a session",
)

_SERVICE_NICHE_KW = (
    "dentist", "dental", "doctor", "clinic", "salon", "barber", "spa",
    "physio", "physiotherap", "plumber", "electrician", "cleaner",
    "mechanic", "lawyer", "solicitor", "accountant", "therapist",
    "chiropractor", "optician", "vet", "veterinar", "gym", "fitness",
    "massage", "tattoo", "nail", "cosmetic", "aesthetic", "restaurant",
    "hairdress", "beauty",
)

_BUILDER_PATTERNS = (
    (r"wix\.com|wixsite|wixstatic", "Wix"),
    (r"squarespace\.com|sqsp\.net|staticwf\.net", "Squarespace"),
    (r"godaddy\.com|secureserver\.net|websitebuilder\.com/", "GoDaddy Website Builder"),
    (r"weebly\.com", "Weebly"),
    (r"jimdo\.com", "Jimdo"),
)

_ANALYTICS_RE = re.compile(
    r"google-analytics\.com|googletagmanager\.com|gtag\("
    r"|ga\.js|analytics\.js|_gaq\.push"
    r"|fbq\(|facebook\.net[^'\"]*pixel"
    r"|hotjar\.com|clarity\.microsoft\.com"
    r"|segment\.com/analytics\.js|mixpanel\.com"
    r"|plausible\.io|fathomanalytics"
)

_COPYRIGHT_RE = re.compile(r"(?:©|copyright|&copy;)\s*((?:19|20)\d{2})")

_WP_GEN_RE = re.compile(
    r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']WordPress\s+([\d.]+)',
    re.I,
)

_SOCIAL_HREF_RE = re.compile(
    r"https?://(?:www\.)?(?:facebook\.com|instagram\.com|twitter\.com|x\.com|"
    r"linkedin\.com|youtube\.com|tiktok\.com)/[a-zA-Z0-9_@./%-]{2,80}",
    re.I,
)

_FREE_EMAIL_RE = re.compile(
    r"\b[\w.+%-]+@(gmail|yahoo|hotmail|outlook|live|icloud|aol|googlemail)"
    r"\.(com|co\.uk|net)\b",
    re.I,
)


# ── Thread-local session ──────────────────────────────────────────────────────

def _get_v2_session() -> requests.Session:
    if not hasattr(_v2_local, "session"):
        s = requests.Session()
        retry = Retry(
            total=2, backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _v2_local.session = s
    return _v2_local.session


# ── Individual check functions ────────────────────────────────────────────────

def _chk_no_ssl(url: str, session: requests.Session) -> tuple[bool, str]:
    parsed = urlparse(url if "://" in url else "https://" + url)
    hostname = parsed.hostname or ""
    try:
        r = session.get(
            f"https://{hostname}", verify=True, timeout=10,
            allow_redirects=False, headers={"User-Agent": _V2_UA},
        )
        if r.is_redirect:
            loc = r.headers.get("location", "")
            if loc.startswith("http://"):
                return True, "Site downgrades HTTPS to plain HTTP — browser shows 'Not Secure'"
        return False, ""
    except requests.exceptions.SSLError as exc:
        msg = str(exc).lower()
        if "expired" in msg:
            return True, "SSL certificate has expired — browser blocks visitors with a security warning"
        if "self signed" in msg or "self-signed" in msg:
            return True, "SSL certificate is self-signed — browser shows a security warning"
        if "hostname" in msg:
            return True, "SSL hostname mismatch — certificate does not match the domain"
        return True, "SSL certificate error — browser shows 'Not Secure'"
    except requests.exceptions.ConnectionError:
        return True, "No HTTPS support — site runs on HTTP only (browser shows 'Not Secure')"
    except Exception:
        return False, ""


def _chk_stale_copyright(html_lower: str) -> tuple[bool, str]:
    current_year = datetime.date.today().year
    for m in _COPYRIGHT_RE.finditer(html_lower):
        year = int(m.group(1))
        if year <= current_year - 2:
            age = current_year - year
            return True, (
                f"Copyright year {year} found in footer "
                f"({age} year{'s' if age != 1 else ''} out of date — site looks abandoned)"
            )
    return False, ""


def _chk_title_meta(soup: BeautifulSoup) -> tuple[bool, str]:
    problems = []
    title_tag = soup.find("title")
    if not title_tag or not (title_tag.string or "").strip():
        problems.append("title tag is missing")
    elif len((title_tag.string or "").strip()) < 15:
        n = len(title_tag.string.strip())
        problems.append(f"title is only {n} chars (< 15 — not useful for Google)")
    desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if not desc or not (desc.get("content") or "").strip():
        problems.append("meta description is missing")
    if problems:
        return True, f"SEO gaps: {'; '.join(problems)}"
    return False, ""


def _chk_missing_h1(soup: BeautifulSoup) -> tuple[bool, str]:
    if not soup.find("h1"):
        return True, "No H1 heading on homepage — weak page structure and SEO signal"
    return False, ""


def _chk_no_schema(soup: BeautifulSoup, html_lower: str) -> tuple[bool, str]:
    if soup.find("script", attrs={"type": "application/ld+json"}):
        return False, ""
    if "itemscope" in html_lower:
        return False, ""
    return True, "No structured data (schema markup) — Google cannot read business details automatically"


def _chk_no_analytics(html_lower: str) -> tuple[bool, str]:
    if _ANALYTICS_RE.search(html_lower):
        return False, ""
    return True, "No analytics or tracking pixel found — no data on website visitors or ad ROI"


def _chk_old_site_builder(html_lower: str, html: str) -> tuple[bool, str]:
    for pattern, name in _BUILDER_PATTERNS:
        if re.search(pattern, html_lower):
            return True, f"Built on {name} (template builder — limited customisation and growth potential)"
    # Old WordPress: wp-content present + generator meta with version ≤ 5
    if "/wp-content/" in html_lower:
        m = _WP_GEN_RE.search(html)
        if m:
            try:
                major = int(m.group(1).split(".")[0])
                if major <= 5:
                    return True, f"WordPress {m.group(1)} detected — outdated version (security risk)"
            except Exception:
                pass
    return False, ""


def _chk_free_email(soup: BeautifulSoup, html_lower: str, biz_domain: str) -> tuple[bool, str]:
    if not biz_domain:
        return False, ""
    # Check mailto: links first
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if href.lower().startswith("mailto:"):
            email = unquote(href[7:].split("?")[0]).strip().lower().lstrip()
            if "@" in email:
                email_domain = email.split("@")[-1]
                if email_domain in _FREE_EMAIL_DOMAINS:
                    return True, (
                        f"Contact email {email} uses a free provider — "
                        f"not professional for a business with its own domain ({biz_domain})"
                    )
    # Fallback: regex scan of page text
    m = _FREE_EMAIL_RE.search(html_lower)
    if m:
        return True, (
            f"Free email address (@{m.group(1)}.{m.group(2)}) found on site — "
            f"looks unprofessional for a business with domain {biz_domain}"
        )
    return False, ""


def _chk_no_booking(html_lower: str, category: str) -> tuple[bool, str]:
    cat = (category or "").lower()
    if not any(kw in cat for kw in _SERVICE_NICHE_KW):
        return False, ""
    if any(p in html_lower for p in _BOOKING_PATTERNS):
        return False, ""
    return True, "No online booking system found (no Calendly, Booksy, Fresha, etc.) — customers cannot self-book"


def _chk_not_mobile_ready(soup: BeautifulSoup) -> tuple[bool, str]:
    vp = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
    if not vp:
        return True, "Missing <meta name='viewport'> — site is not configured for mobile devices"
    return False, ""


def _chk_no_chatbot(html_lower: str) -> tuple[bool, str]:
    for sig in CHATBOT_SIGNATURES:
        if sig in html_lower:
            return False, ""
    return True, "No live chat or AI chatbot detected — 24/7 customer engagement is missing"


def _chk_outdated_tech(html_lower: str) -> tuple[bool, str]:
    found = []
    for pattern, label in OUTDATED_SIGNATURES:
        if re.search(pattern, html_lower) and label not in found:
            found.append(label)
    if found:
        return True, f"Outdated technology: {', '.join(found[:3])} — security risk and slow performance"
    return False, ""


def _chk_no_contact_form(soup: BeautifulSoup) -> tuple[bool, str]:
    for form in soup.find_all("form"):
        if len(form.find_all("input")) >= 2:
            return False, ""
    return True, "No contact form found — visitors have no easy way to enquire online"


def _chk_broken_links(
    base_url: str, soup: BeautifulSoup, session: requests.Session
) -> tuple[bool, str]:
    parsed = urlparse(base_url)
    base_host = parsed.netloc.lower()
    candidates: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        full = urljoin(base_url, href)
        p = urlparse(full)
        if p.netloc.lower() != base_host:
            continue
        path = p.path.rstrip("/")
        if not path or path == parsed.path.rstrip("/"):
            continue
        candidates.add(full)
        if len(candidates) >= 15:
            break
    if not candidates:
        return False, ""
    broken: list[tuple[str, object]] = []
    for link in candidates:
        try:
            r = session.head(link, verify=False, timeout=8, allow_redirects=True,
                             headers={"User-Agent": _V2_UA})
            if r.status_code >= 400:
                broken.append((link, r.status_code))
        except Exception:
            pass  # timeout = network issue, not site issue
    if broken:
        n = len(broken)
        ex_path = urlparse(broken[0][0]).path or broken[0][0]
        return True, (
            f"{n} of {len(candidates)} sampled internal links returned 4xx "
            f"(e.g. {ex_path} → {broken[0][1]}) — broken pages visible to visitors"
        )
    return False, ""


def _chk_dead_socials(soup: BeautifulSoup, session: requests.Session) -> tuple[bool, str]:
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if _SOCIAL_HREF_RE.match(href):
            links.append(href)
    links = list(dict.fromkeys(links))[:5]  # dedup, max 5
    if not links:
        return False, ""
    dead: list[str] = []
    for link in links:
        try:
            r = session.head(link, verify=False, timeout=10, allow_redirects=True,
                             headers={"User-Agent": _V2_UA})
            if r.status_code in (404, 410):
                dead.append(link)
        except Exception:
            pass  # network error — don't count as dead
    if dead:
        short = [urlparse(d).path.strip("/")[:40] or d for d in dead[:2]]
        return True, (
            f"{len(dead)} social media link(s) returned 404 "
            f"(dead profiles: {', '.join(short)})"
        )
    return False, ""


# ── PageSpeed v2 with 30-day DB cache ─────────────────────────────────────────

def _get_pagespeed_v2(
    url: str, domain: str, api_key: str, session: requests.Session
) -> dict | None:
    """
    Returns dict with mobile_score, desktop_score, fcp_s, lcp_s,
    total_byte_weight, viewport_ok. Checks DB cache first (30-day TTL).

    Calls are serialized (one at a time) with a minimum inter-call gap to
    avoid 429 rate-limit errors when running 8 audit workers in parallel.
    """
    from database import get_pagespeed_cache, save_pagespeed_cache

    if domain:
        cached = get_pagespeed_cache(domain)
        if cached:
            print(f"    [PageSpeed] cache hit: {domain}", flush=True)
            return cached

    label = domain or url
    print(f"    [PageSpeed] calling for {label}", flush=True)

    # Serialize: one call at a time; enforce minimum gap to avoid 429.
    # Without an API key: 4 s gap (free quota is 25/day shared by IP).
    # With a key: 1.5 s gap (25 000/day — bursting is fine).
    gap = 1.5 if api_key else 4.0

    with _pagespeed_sem:
        elapsed = time.monotonic() - _pagespeed_prev_t[0]
        if elapsed < gap:
            time.sleep(gap - elapsed)
        _pagespeed_prev_t[0] = time.monotonic()

        data: dict = {}
        for strategy in ("mobile", "desktop"):
            params: dict = {"url": url, "strategy": strategy}
            if api_key:
                params["key"] = api_key
            try:
                r = session.get(
                    PAGESPEED_ENDPOINT, params=params, timeout=45,
                    headers={"User-Agent": _V2_UA},
                )
                if r.status_code == 429:
                    print(
                        f"    [PageSpeed] RATE LIMITED (429) for {label} — "
                        "add PAGESPEED_API_KEY to .env for higher quota",
                        flush=True,
                    )
                    return None
                if r.status_code != 200:
                    print(
                        f"    [PageSpeed] HTTP {r.status_code} ({strategy}) for {label}",
                        flush=True,
                    )
                    continue
                body   = r.json()
                cats   = body.get("lighthouseResult", {}).get("categories", {})
                audits = body.get("lighthouseResult", {}).get("audits", {})
                score  = round((cats.get("performance", {}).get("score") or 0) * 100)
                if strategy == "mobile":
                    data["mobile_score"] = score
                    vp = audits.get("viewport", {})
                    data["viewport_ok"]  = 1 if (vp.get("score") == 1) else 0
                else:
                    data["desktop_score"] = score
                    data["fcp_s"]  = _extract_seconds(
                        audits.get("first-contentful-paint", {}).get("displayValue", "")
                    )
                    data["lcp_s"]  = _extract_seconds(
                        audits.get("largest-contentful-paint", {}).get("displayValue", "")
                    )
                    tbw = audits.get("total-byte-weight", {})
                    if tbw.get("numericValue"):
                        data["total_byte_weight"] = int(tbw["numericValue"])
            except Exception as exc:
                print(f"    [PageSpeed] FAILED ({strategy}): {exc}", flush=True)
                continue

    if not data:
        print(f"    [PageSpeed] no data returned for {label}", flush=True)
        return None

    mob  = data.get("mobile_score", "?")
    desk = data.get("desktop_score", "?")
    print(f"    [PageSpeed] {label}: mobile={mob} desktop={desk}", flush=True)

    if domain:
        try:
            save_pagespeed_cache(domain, data)
        except Exception:
            pass
    return data


# ── Main v2 entry point ───────────────────────────────────────────────────────

def audit_site(
    url: str,
    biz_name: str = "",
    category: str = "",
    domain: str = "",
    pagespeed_api_key: str = "",
) -> dict[str, tuple[bool, str]]:
    """
    v2 deep audit of one website.

    Returns dict keyed by check name → (triggered: bool, evidence: str).

    Special key ``site_unreachable`` (not in scoring weights) is returned
    when the network itself prevents a connection (timeout, DNS failure, etc.).
    In that case the caller should NOT update the business status so the
    business can be retried on the next run.
    """
    session = _get_v2_session()
    results: dict[str, tuple[bool, str]] = {}

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed_orig = urlparse(url)
    hostname = parsed_orig.hostname or ""
    hostname_bare = re.sub(r"^www\.", "", hostname)

    # ── 1. Fetch homepage — try HTTPS then HTTP, www then bare ───────────────
    resp = soup = html = html_lower = None
    fetch_err = ""

    for try_url in [url, f"https://{hostname_bare}", f"http://{hostname_bare}"]:
        try:
            r = session.get(
                try_url, verify=False, timeout=12, allow_redirects=True,
                headers={"User-Agent": _V2_UA}, stream=True,
            )
            content = b""
            for chunk in r.iter_content(8192):
                content += chunk
                if len(content) >= 600_000:
                    break
            r.close()
            html = content.decode("utf-8", errors="ignore")
            html_lower = html.lower()
            soup = BeautifulSoup(html, "html.parser")
            resp = r
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectTimeout):
            fetch_err = "connection timed out"
        except requests.exceptions.ConnectionError:
            fetch_err = "connection refused or DNS failure"
        except Exception as exc:
            fetch_err = str(exc)[:120]

    if resp is None:
        return {"site_unreachable": (True, f"Could not connect to site ({fetch_err})")}

    # ── 2. site_down ─────────────────────────────────────────────────────────
    if resp.status_code >= 400:
        results["site_down"] = (
            True,
            f"Site returned HTTP {resp.status_code} — not accessible to visitors",
        )
        # Fill remaining keys as untriggered so callers can iterate uniformly
        for k in (
            "no_ssl", "stale_copyright", "missing_title_meta", "missing_h1",
            "no_schema", "no_analytics", "old_site_builder", "free_email_domain",
            "no_booking_system", "not_mobile_ready", "no_chatbot", "outdated_tech",
            "no_contact_form", "dead_social_links", "broken_links",
            "mobile_score_lt_40", "mobile_score_lt_60", "lcp_gt_4s", "large_page_weight",
        ):
            results.setdefault(k, (False, ""))
        return results
    results["site_down"] = (False, "")

    # ── 3. SSL check (separate request with verify=True) ─────────────────────
    try:
        results["no_ssl"] = _chk_no_ssl(url, session)
    except Exception:
        results["no_ssl"] = (False, "")

    # ── 4. Cheap HTML checks (no extra network calls) ─────────────────────────
    html_checks = {
        "stale_copyright":   lambda: _chk_stale_copyright(html_lower),
        "missing_title_meta": lambda: _chk_title_meta(soup),
        "missing_h1":        lambda: _chk_missing_h1(soup),
        "no_schema":         lambda: _chk_no_schema(soup, html_lower),
        "no_analytics":      lambda: _chk_no_analytics(html_lower),
        "old_site_builder":  lambda: _chk_old_site_builder(html_lower, html),
        "free_email_domain": lambda: _chk_free_email(soup, html_lower, domain),
        "no_booking_system": lambda: _chk_no_booking(html_lower, category),
        "not_mobile_ready":  lambda: _chk_not_mobile_ready(soup),
        "no_chatbot":        lambda: _chk_no_chatbot(html_lower),
        "outdated_tech":     lambda: _chk_outdated_tech(html_lower),
        "no_contact_form":   lambda: _chk_no_contact_form(soup),
    }
    for key, fn in html_checks.items():
        try:
            results[key] = fn()
        except Exception:
            results[key] = (False, "")

    # ── 5. Network checks on sampled links / socials ──────────────────────────
    for key, fn in {
        "dead_social_links": lambda: _chk_dead_socials(soup, session),
        "broken_links":      lambda: _chk_broken_links(url, soup, session),
    }.items():
        try:
            results[key] = fn()
        except Exception:
            results[key] = (False, "")

    # ── 6. PageSpeed — only when ≥1 cheap issue already found ─────────────────
    for k in ("mobile_score_lt_40", "mobile_score_lt_60", "lcp_gt_4s", "large_page_weight"):
        results[k] = (False, "")

    cheap_triggered = sum(1 for k, (t, _) in results.items() if t)
    if cheap_triggered >= 1:
        try:
            ps = _get_pagespeed_v2(url, domain, pagespeed_api_key, session)
            if ps:
                mobile = ps.get("mobile_score")
                if mobile is not None:
                    if mobile < 40:
                        results["mobile_score_lt_40"] = (
                            True,
                            f"Mobile performance score is {mobile}/100 — site is nearly unusable on phones",
                        )
                    elif mobile < 60:
                        results["mobile_score_lt_60"] = (
                            True,
                            f"Mobile performance score is {mobile}/100 — poor phone experience, hurts Google ranking",
                        )
                lcp = ps.get("lcp_s")
                if lcp and lcp > 4.0:
                    results["lcp_gt_4s"] = (
                        True,
                        f"LCP is {lcp:.1f}s (Google recommends < 2.5s) — most phone visitors leave before the page loads",
                    )
                tbw = ps.get("total_byte_weight")
                if tbw and tbw > 5_000_000:
                    mb = tbw / 1_000_000
                    results["large_page_weight"] = (
                        True,
                        f"Page size is {mb:.1f} MB (> 5 MB) — extremely slow on mobile data",
                    )
                if ps.get("viewport_ok") == 0 and not results["not_mobile_ready"][0]:
                    results["not_mobile_ready"] = (
                        True,
                        "PageSpeed confirms site is not mobile-friendly (viewport audit failed)",
                    )
        except Exception:
            pass

    return results
