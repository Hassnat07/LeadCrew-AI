import os
import ssl
import smtplib
import imaplib
import re
import time
import logging
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from dataclasses import dataclass
from typing import List, Optional

try:
    import dns.resolver as _dns_resolver
    _DNS_AVAILABLE = True
except ImportError:
    _DNS_AVAILABLE = False

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Email verification ────────────────────────────────────────────────────────


def get_mx(domain: str):
    """Return (preference, hostname) MX pairs for domain, sorted lowest-preference-first."""
    if not _DNS_AVAILABLE:
        return []
    try:
        recs = _dns_resolver.resolve(domain, "MX")
        return sorted((r.preference, str(r.exchange).rstrip(".")) for r in recs)
    except Exception:
        return []


def _verify_via_smtp(
    email: str,
    helo: str = "haxantech.com",
    probe_from: str = "verify@haxantech.com",
) -> Optional[bool]:
    """
    SMTP RCPT-TO probe on port 25.
    Returns True (accepted), False (server rejected address), or None (could not connect
    — port 25 likely blocked by ISP/host; caller should fall back to API).
    """
    domain = email.split("@")[1]
    mx_records = get_mx(domain)
    if not mx_records:
        return False
    try:
        s = smtplib.SMTP(timeout=10)
        s.connect(mx_records[0][1], 25)
        s.helo(helo)
        s.mail(probe_from)
        code, _ = s.rcpt(email)
        s.quit()
        return code in (250, 251)
    except (ConnectionRefusedError, TimeoutError, OSError) as e:
        # Connection-level failure — can't tell if address is valid, port likely blocked
        logger.debug("Port 25 unreachable probing %s: %s", email, e)
        return None
    except smtplib.SMTPException as e:
        # SMTP protocol error — address was actively rejected
        logger.debug("SMTP rejection probing %s: %s", email, e)
        return False
    except Exception as e:
        logger.debug("SMTP probe failed for %s: %s", email, e)
        return None


def _verify_via_api(email: str) -> Optional[bool]:
    """
    Verify via ZeroBounce or Reoon API.
    Returns True/False, or None if no API key is configured.
    Set ZEROBOUNCE_API_KEY or REOON_API_KEY in environment.
    """
    if not _REQUESTS_AVAILABLE:
        return None

    zb_key = os.getenv("ZEROBOUNCE_API_KEY", "")
    if zb_key:
        try:
            resp = _requests.get(
                "https://api.zerobounce.net/v2/validate",
                params={"api_key": zb_key, "email": email},
                timeout=10,
            )
            status = resp.json().get("status", "").lower()
            logger.debug("ZeroBounce %s → %s", email, status)
            return status == "valid"
        except Exception as e:
            logger.debug("ZeroBounce request failed for %s: %s", email, e)
            return None

    reoon_key = os.getenv("REOON_API_KEY", "")
    if reoon_key:
        try:
            resp = _requests.get(
                "https://emailverifier.reoon.com/api/v1/verify",
                params={"email": email, "key": reoon_key, "mode": "quick"},
                timeout=10,
            )
            status = resp.json().get("status", "").lower()
            logger.debug("Reoon %s → %s", email, status)
            return status == "valid"
        except Exception as e:
            logger.debug("Reoon request failed for %s: %s", email, e)
            return None

    return None


def verify_email(email: str) -> bool:
    """
    Gate before sending: returns True if safe to send, False to skip.

    Controlled by EMAIL_VERIFY_METHOD env var (default: smtp):
      smtp  — SMTP RCPT-TO probe on port 25; falls back to API when port is blocked
      api   — API-only (requires ZEROBOUNCE_API_KEY or REOON_API_KEY)
      none  — bypass verification entirely (not recommended for cold outreach)
    """
    if not email or "@" not in email:
        return False
    if not re.match(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", email):
        return False

    method = os.getenv("EMAIL_VERIFY_METHOD", "smtp").lower()

    if method == "none":
        return True

    if method == "api":
        result = _verify_via_api(email)
        if result is None:
            logger.warning(
                "EMAIL_VERIFY_METHOD=api but no API key configured; "
                "allowing %s through unverified", email,
            )
            return True
        return result


    # Default: smtp with automatic API fallback when port 25 is blocked
    smtp_result = _verify_via_smtp(email)
    if smtp_result is True:
        return True
    if smtp_result is False:
        return False
    # None → port 25 unreachable; escalate to API
    api_result = _verify_via_api(email)
    if api_result is None:
        logger.warning(
            "Port 25 blocked and no API key configured; cannot verify %s — allowing through", email,
        )
        return True  # Can't verify at all; allow rather than silently block every send
    return api_result


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class ParsedEmail:
    company: str
    subject: str
    body: str
    to_address: Optional[str] = None


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_emails_from_result(result_text: str) -> List[ParsedEmail]:
    emails = []
    pattern = r'Email\s+\d+\s*[—\-:]+\s*([^\n]+)'
    splits = re.split(pattern, result_text)

    company_names = []
    body_blocks = []

    for i, part in enumerate(splits):
        if i == 0:
            continue
        if i % 2 == 1:
            company_names.append(part.strip())
        else:
            body_blocks.append(part.strip())

    for company, block in zip(company_names, body_blocks):
        to_match = re.search(r'^To:\s*([^\s]+@[^\s]+\.[^\s]+)', block, re.IGNORECASE | re.MULTILINE)
        to_address = to_match.group(1).strip() if to_match else None
        if to_match:
            block = block[:to_match.start()] + block[to_match.end():]

        subject_match = re.search(r'Subject:\s*(.+)', block, re.IGNORECASE)
        if subject_match:
            subject = subject_match.group(1).strip()
            body = block[subject_match.end():].strip()
        else:
            subject = f"Quick question for {company}"
            body = block.strip()

        body = re.sub(r'^Body:\s*', '', body, flags=re.IGNORECASE).strip()
        emails.append(ParsedEmail(company=company, subject=subject, body=body, to_address=to_address))

    if not emails:
        emails.append(ParsedEmail(
            company="All Prospects",
            subject="Outreach from Haxantech",
            body=result_text,
        ))

    return emails


# ── IMAP Sent copy ────────────────────────────────────────────────────────────

def _save_to_sent(imap_host: str, imap_port: int, email_addr: str, password: str, raw_msg: bytes):
    """Copy sent message to the IMAP Sent folder (best-effort; never raises)."""
    try:
        if imap_port == 993:
            server = imaplib.IMAP4_SSL(imap_host, imap_port)
        else:
            server = imaplib.IMAP4(imap_host, imap_port)
            server.starttls()
        server.login(email_addr, password)
        for folder in ("Sent", "INBOX.Sent", "Sent Items", "Sent Messages"):
            try:
                result = server.append(
                    folder, "\\Seen",
                    imaplib.Time2Internaldate(time.time()),
                    raw_msg,
                )
                if result[0] == "OK":
                    break
            except Exception:
                continue
        server.logout()
    except Exception:
        pass


# ── Send ──────────────────────────────────────────────────────────────────────

def send_single_email(
    smtp_host: str,
    smtp_port: int,
    sender_email: str,
    sender_password: str,
    to_address: str,
    subject: str,
    body: str,
    from_name: str = "",
    sender_company: str = "",
    imap_host: str = "",
    imap_port: int = 993,
    use_ssl: Optional[bool] = None,
) -> dict:
    """
    Send one plain-text cold email.
    Plain text only — no HTML template — to avoid spam/bulk-mail filters.

    use_ssl controls the connection mode:
      True  — implicit TLS (SMTP_SSL), required for port 465 / cPanel
      False — STARTTLS upgrade, standard for port 587
      None  — auto: port 465 → SSL, everything else → STARTTLS
    """
    # Resolve SSL mode: explicit flag overrides port-based detection
    _use_ssl = use_ssl if use_ssl is not None else (smtp_port == 465)

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"]    = subject
        msg["From"]       = f"{from_name} <{sender_email}>" if from_name else sender_email
        msg["To"]         = to_address
        msg["Reply-To"]   = sender_email
        msg["Date"]       = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="haxantech.com")

        raw_msg = msg.as_bytes()

        if _use_ssl:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=30) as server:
                server.login(sender_email, sender_password)
                server.sendmail(sender_email, [to_address], raw_msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.login(sender_email, sender_password)
                server.sendmail(sender_email, [to_address], raw_msg)

        if imap_host:
            _save_to_sent(imap_host, imap_port, sender_email, sender_password, raw_msg)

        return {"success": True}

    except smtplib.SMTPAuthenticationError:
        return {"success": False, "error": "Authentication failed — check your email address and password."}
    except smtplib.SMTPRecipientsRefused:
        return {"success": False, "error": f"Recipient '{to_address}' was refused by the server."}
    except smtplib.SMTPConnectError:
        return {"success": False, "error": f"Could not connect to {smtp_host}:{smtp_port}. Check your SMTP settings."}
    except TimeoutError:
        return {"success": False, "error": f"Connection to {smtp_host} timed out."}
    except Exception as e:
        return {"success": False, "error": str(e)}


def test_smtp_connection(
    smtp_host: str, smtp_port: int,
    sender_email: str, sender_password: str,
    sender_name: str = "Haxantech",
    sender_company: str = "",
    use_ssl: Optional[bool] = None,
) -> dict:
    """Send a test email to the sender themselves to verify SMTP works."""
    result = send_single_email(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        sender_email=sender_email,
        sender_password=sender_password,
        to_address=sender_email,
        subject=f"{sender_company or sender_name} — SMTP connection test",
        body=(
            f"This is a test email from {sender_company or sender_name}.\n\n"
            "If you received this, your email setup is working correctly!"
        ),
        from_name=sender_name,
        sender_company=sender_company,
        imap_host=smtp_host,
        imap_port=993,
        use_ssl=use_ssl,
    )
    # Annotate with the connection mode used so the dashboard can display it
    _use_ssl = use_ssl if use_ssl is not None else (smtp_port == 465)
    result["mode"] = "SSL/TLS" if _use_ssl else "STARTTLS"
    result["host"] = smtp_host
    result["port"] = smtp_port
    return result


DAILY_LIMIT = 150


def send_bulk_emails(
    sender_email: str,
    sender_password: str,
    emails: List[ParsedEmail],
    from_name: str = "",
    sender_company: str = "",
    smtp_host: str = "",
    smtp_port: int = 465,
    imap_host: str = "",
    imap_port: int = 993,
    send_delay: float = 5.0,
    campaign: str = "",
    daily_limit: int = DAILY_LIMIT,
) -> List[dict]:
    """
    Send emails with verification, per-send delay, daily-limit enforcement, and deduplication.
    Skips any address that fails verify_email().
    """
    from database import already_contacted, log_sent, today_count

    results: List[dict] = []
    sent_today = today_count()

    for i, email in enumerate(emails):
        # ── No recipient ──────────────────────────────────────────────────────
        if not email.to_address:
            results.append({
                "company": email.company,
                "success": False,
                "skipped": True,
                "error": "No recipient email address.",
            })
            continue

        # ── Daily limit ───────────────────────────────────────────────────────
        if sent_today >= daily_limit:
            results.append({
                "company": email.company,
                "success": False,
                "skipped": True,
                "error": f"Daily limit of {daily_limit} reached for today.",
            })
            continue

        # ── Deduplication ─────────────────────────────────────────────────────
        if already_contacted(email.to_address):
            results.append({
                "company": email.company,
                "success": False,
                "skipped": True,
                "error": "Already contacted — skipped to avoid duplicate.",
            })
            continue

        # ── Email verification ─────────────────────────────────────────────────
        if not verify_email(email.to_address):
            logger.info(
                "Skipped %s (%s): failed email verification.",
                email.company, email.to_address,
            )
            results.append({
                "company": email.company,
                "success": False,
                "skipped": True,
                "error": f"{email.to_address} failed verification — address likely does not exist.",
            })
            continue

        # ── Send ──────────────────────────────────────────────────────────────
        result = send_single_email(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            sender_email=sender_email,
            sender_password=sender_password,
            to_address=email.to_address,
            subject=email.subject,
            body=email.body,
            from_name=from_name,
            sender_company=sender_company,
            imap_host=imap_host,
            imap_port=imap_port,
        )
        result["company"] = email.company
        result["skipped"] = False

        if result["success"]:
            log_sent(email.company, email.to_address, email.subject, campaign)
            sent_today += 1

        results.append(result)

        if send_delay > 0 and i < len(emails) - 1:
            time.sleep(send_delay)

    return results


# ── Dashboard send ────────────────────────────────────────────────────────────

def send_approved_draft(draft_id: int) -> dict:
    """
    Send one approved draft from the email_drafts table.

    Enforces (in order): send gap (3-10 min random), daily cap, dedup,
    email verification, then SMTP send.  SMTP config read from env:
      SMTP_HOST, SMTP_PORT (default 465), SMTP_EMAIL, SMTP_PASSWORD,
      SMTP_FROM_NAME (default 'Hassnat'), IMAP_HOST, IMAP_PORT (default 993).

    Returns {success, skipped, error, wait_seconds (if gap not elapsed)}.
    """
    import random
    import datetime as _dt
    from database import (
        get_draft, update_draft_status,
        log_sent, already_contacted, today_count,
        log_followup_needed, get_send_gap, set_send_gap,
    )

    draft = get_draft(draft_id)
    if not draft:
        return {"success": False, "skipped": False, "error": "Draft not found"}

    to_email = draft.get("to_email")
    if not to_email:
        return {"success": False, "skipped": True, "error": "No recipient email on this draft"}

    # Send-gap check
    gap = get_send_gap()
    next_ok = gap.get("next_ok_at")
    if next_ok:
        try:
            diff = (_dt.datetime.fromisoformat(next_ok) - _dt.datetime.now()).total_seconds()
            if diff > 0:
                wait_s = int(diff)
                return {
                    "success": False, "skipped": True,
                    "error": f"Too soon — {wait_s // 60}m {wait_s % 60}s until next send",
                    "wait_seconds": wait_s,
                }
        except Exception:
            pass

    # Daily cap
    if today_count() >= DAILY_LIMIT:
        return {"success": False, "skipped": True, "error": f"Daily limit of {DAILY_LIMIT} reached"}

    # Dedup
    if already_contacted(to_email):
        update_draft_status(draft_id, "skipped")
        return {"success": False, "skipped": True, "error": "Already contacted this address"}

    # Email verification
    if not verify_email(to_email):
        update_draft_status(draft_id, "bad_email")
        return {"success": False, "skipped": True, "error": f"{to_email} failed verification"}

    # SMTP config from env
    smtp_host  = os.getenv("SMTP_HOST", "")
    smtp_port  = int(os.getenv("SMTP_PORT", "465"))
    s_email    = os.getenv("SMTP_EMAIL", "")
    s_password = os.getenv("SMTP_PASSWORD", "")
    from_name  = os.getenv("SMTP_FROM_NAME", "Hassnat")
    imap_host  = os.getenv("IMAP_HOST", smtp_host)
    imap_port  = int(os.getenv("IMAP_PORT", "993"))
    _ssl_env   = os.getenv("SMTP_SSL", "").lower()
    use_ssl    = True if _ssl_env == "true" else (False if _ssl_env == "false" else None)

    if not smtp_host or not s_email or not s_password:
        return {"success": False, "skipped": False, "error": "SMTP not configured — check .env"}

    result = send_single_email(
        smtp_host=smtp_host, smtp_port=smtp_port,
        sender_email=s_email, sender_password=s_password,
        to_address=to_email,
        subject=draft["subject"], body=draft["body"],
        from_name=from_name,
        imap_host=imap_host, imap_port=imap_port,
        use_ssl=use_ssl,
    )

    if result["success"]:
        now_str = _dt.datetime.now().isoformat()
        log_sent(draft.get("biz_name", ""), to_email, draft["subject"], campaign="v2-dashboard")
        update_draft_status(draft_id, "sent")
        log_followup_needed(draft["business_id"], draft_id, now_str)
        gap_s     = random.randint(30, 60)
        next_ok_s = (_dt.datetime.now() + _dt.timedelta(seconds=gap_s)).isoformat()
        set_send_gap(now_str, next_ok_s)

    result.setdefault("wait_seconds", None)
    return result


# ── SMTP / IMAP Presets ───────────────────────────────────────────────────────
SMTP_PRESETS = {
    "cPanel SSL (port 465)": {
        "host": "srv31.easyhost.pk", "port": 465,
        "imap_host": "srv31.easyhost.pk", "imap_port": 993,
        "note": "Use your cPanel email password",
    },
    "cPanel STARTTLS (port 587)": {
        "host": "srv31.easyhost.pk", "port": 587,
        "imap_host": "srv31.easyhost.pk", "imap_port": 993,
        "note": "Try this if port 465 times out — same password",
    },
    "Gmail": {
        "host": "smtp.gmail.com", "port": 465,
        "imap_host": "imap.gmail.com", "imap_port": 993,
        "note": "Use a Gmail App Password (not your regular password)",
    },
    "Outlook / Hotmail": {
        "host": "smtp-mail.outlook.com", "port": 587,
        "imap_host": "outlook.office365.com", "imap_port": 993,
        "note": "Use your Outlook password",
    },
    "Custom": {
        "host": "", "port": 465,
        "imap_host": "", "imap_port": 993,
        "note": "Enter your SMTP server manually",
    },
}