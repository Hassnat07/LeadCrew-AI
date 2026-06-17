"""
LeadCrew AI v2 — Send Dashboard

Run:  streamlit run dashboard.py

Tabs:
  New Search         — harvest any niche + location from the UI
  Website Issues     — email drafts for website-problem leads (UK/USA/Italy)
  Follow-ups         — due follow-up drafts
  No Website Leads   — no-website leads split by: No Website / Social Only
  Sent History       — last 50 sends
"""

import csv
import html as _html
import io
import json
import os
import sys
import threading
import time
from datetime import datetime
from urllib.parse import quote as _urlquote

import streamlit.components.v1 as components

# Force UTF-8 on Windows so emoji in business names don't crash print()
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except OSError:
    pass
try:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except OSError:
    pass

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _harvest_state as hs

from database import (
    _init, _conn, get_leads_with_drafts, get_no_website_leads_full,
    get_all_followups, get_send_gap, today_count, recent_sent,
    update_draft_status, log_search, get_search_history,
    get_draft_dates, delete_drafts_by_date, cleanup_stale_leads,
)
from email_sender import send_approved_draft, test_smtp_connection, DAILY_LIMIT

_init()

# ── Country routing ───────────────────────────────────────────────────────────

COUNTRY_OPTIONS: dict[str, dict] = {
    "Pakistan": {"suffix": ", Pakistan", "market": "whatsapp"},
    "UK":       {"suffix": ", UK",       "market": "email"},
    "Italy":    {"suffix": ", Italy",    "market": "email"},
    "USA":      {"suffix": ", USA",      "market": "email"},
    "Other":    {"suffix": "",           "market": "email"},
}

RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "3"))


# ── Background pipeline ───────────────────────────────────────────────────────

def _run_pipeline(niche: str, city_clean: str, city_serper: str,
                  country: str, market: str, target: int = 20) -> None:
    api_key    = os.getenv("SERPER_API_KEY", "")
    openai_key = os.getenv("OPENAI_API_KEY", "")

    if not api_key:
        hs.append_log("Error: SERPER_API_KEY not set in .env — aborting")
        hs.fail("SERPER_API_KEY not set")
        return

    MAX_ATTEMPTS = 4  # original niche + up to 3 LLM-suggested synonyms

    try:
        hs.append_log(f"Harvesting **{niche}** in {city_serper} (target: {target} new leads)…")
        from harvester import harvest, expand_niche_synonyms
        from audit_runner import run_audit
        from scoring import score_all

        with _conn() as c:
            max_id_before = (
                c.execute("SELECT COALESCE(MAX(id),0) FROM seen_businesses")
                .fetchone()[0]
            )

        queries       = [niche]
        attempt       = 0
        new_count     = 0
        new_qualified = 0
        db_total_qualified = 0

        while attempt < len(queries) and attempt < MAX_ATTEMPTS:
            query_niche = queries[attempt]
            if attempt > 0:
                hs.append_log(f"Still short of target ({new_qualified}/{target}) — trying **{query_niche}**…")

            added      = harvest(query_niche, city_serper, api_key)
            batch_new  = len(added)
            new_count += batch_new
            if batch_new == 0:
                hs.append_log(
                    "   → 0 new businesses (all results already in your database — dedup working)"
                )
            else:
                hs.append_log(f"   → {batch_new} new businesses saved")

            with _conn() as c:
                c.execute(
                    "UPDATE seen_businesses SET market = ? "
                    "WHERE id > ? AND (market IS NULL OR market = '')",
                    (market, max_id_before),
                )

            hs.append_log("Auditing websites…")
            audit_stats = run_audit()
            hs.append_log(
                f"   → {audit_stats['audited']} audited, "
                f"{audit_stats.get('unreachable', 0)} unreachable"
            )

            hs.append_log("Scoring leads…")
            score_stats = score_all(verbose=False)
            db_total_qualified = score_stats.get("qualified", 0)

            with _conn() as c:
                new_qualified = c.execute(
                    "SELECT COUNT(*) FROM seen_businesses "
                    "WHERE id > ? AND status = 'qualified'",
                    (max_id_before,),
                ).fetchone()[0]

            hs.append_log(
                f"   → {new_qualified}/{target} new qualified so far  |  "
                f"{db_total_qualified} total in DB"
            )

            attempt += 1

            if new_qualified >= target:
                break

            # Still short — widen the search with LLM-suggested niche synonyms,
            # once, if we have a key to ask with.
            if len(queries) == 1 and attempt < MAX_ATTEMPTS and openai_key:
                hs.append_log("Looking for similar niches to widen the search…")
                synonyms = expand_niche_synonyms(niche, openai_key, n=MAX_ATTEMPTS - 1)
                synonyms = [s for s in synonyms if s.strip().lower() != niche.strip().lower()]
                if synonyms:
                    queries.extend(synonyms[:MAX_ATTEMPTS - 1])
                    hs.append_log("   → Trying: " + ", ".join(f'"{s}"' for s in queries[1:]))

        if new_qualified < target:
            hs.append_log(
                f"   → Reached the limit of available businesses for \"{niche}\" in "
                f"{city_serper} — found {new_qualified}/{target}. Try a different city for more."
            )

        if openai_key:
            hs.append_log("Enriching contacts (website scrape + social profiles)…")
            from enrich_contacts import enrich_all
            enrich_all(limit=target, workers=4)
            hs.append_log("Writing email drafts for leads with email…")
            from write_emails import write_emails
            write_emails(limit=target, api_key=openai_key)
            hs.append_log(
                "   → Leads with email → **Website Issues** tab  |  "
                "No email → **No Website Leads** tab"
            )
        else:
            hs.append_log("Warning: OPENAI_API_KEY not set — skipping enrichment and drafts")

        log_search(niche, city_clean, country, market, new_qualified)
        hs.finish({
            "harvested":       new_count,
            "new_qualified":   new_qualified,
            "total_qualified": db_total_qualified,
            "market":          market,
        })
        hs.append_log(
            f"**Pipeline complete** — "
            f"{new_qualified} new leads this search  |  {db_total_qualified} total in DB"
        )

    except Exception as exc:
        hs.append_log(f"Error: {exc}")
        hs.fail(str(exc))


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="LeadCrew AI — Dashboard",
    page_icon=":material/person_search:",
    layout="wide",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700;800&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

/* ── Design tokens ── */
:root {
  --bg-base:      #07090F;
  --bg-panel:     #0E1320;
  --bg-elevated:  #141B2D;
  --border:       rgba(255,255,255,0.06);
  --border-hi:    rgba(255,255,255,0.12);
  --accent:       #E8E14C;
  --accent-2:     #9FD13C;
  --success:      #2BD98A;
  --warn:         #F5A524;
  --danger:       #FF5C7C;
  --text-primary: #E8EAF0;
  --text-muted:   #6B7280;
}

/* ── Chrome ── */
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding-top: 1rem !important; max-width: 100% !important; }

/* ── Base ── */
.stApp {
  background: var(--bg-base) !important;
  font-family: 'Inter', sans-serif !important;
  color: var(--text-primary) !important;
}

/* ── Sidebar ── */
[data-testid="stSidebar"] {
  background: linear-gradient(180deg, var(--bg-panel) 0%, #090c15 100%) !important;
  border-right: 1px solid var(--border) !important;
}
[data-testid="stSidebar"] .stMarkdown p,
[data-testid="stSidebar"] .stCaption {
  color: var(--text-muted) !important;
  font-size: 0.78rem !important;
}

/* ── Metric tiles ── */
[data-testid="stMetric"] {
  background: linear-gradient(135deg, rgba(232,225,76,0.05) 0%, rgba(159,209,60,0.05) 100%) !important;
  border: 1px solid var(--border-hi) !important;
  border-top: 1px solid rgba(255,255,255,0.14) !important;
  border-radius: 12px !important;
  padding: 14px 16px !important;
  box-shadow: 0 2px 16px rgba(0,0,0,0.35) !important;
  transition: box-shadow 0.25s ease !important;
}
[data-testid="stMetric"]:hover {
  box-shadow: 0 4px 28px rgba(232,225,76,0.12) !important;
}
[data-testid="stMetricValue"] {
  font-family: 'Space Grotesk', sans-serif !important;
  font-weight: 700 !important;
  font-size: 1.6rem !important;
  color: var(--accent) !important;
  text-shadow: 0 0 18px rgba(232,225,76,0.45) !important;
}
[data-testid="stMetricLabel"] {
  font-family: 'Inter', sans-serif !important;
  color: var(--text-muted) !important;
  font-weight: 500 !important;
  text-transform: uppercase !important;
  letter-spacing: 1.5px !important;
  font-size: 0.65rem !important;
}

/* ── Bordered containers ── */
[data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--bg-panel) !important;
  border: 1px solid var(--border) !important;
  border-top: 1px solid var(--border-hi) !important;
  border-radius: 14px !important;
  box-shadow: 0 4px 20px rgba(0,0,0,0.28) !important;
  transition: box-shadow 0.2s ease, border-color 0.2s ease, transform 0.2s ease !important;
  backdrop-filter: blur(8px) !important;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover {
  border-color: rgba(232,225,76,0.22) !important;
  box-shadow: 0 8px 32px rgba(232,225,76,0.07), 0 4px 20px rgba(0,0,0,0.35) !important;
  transform: translateY(-1px) !important;
}

/* ── Tabs ── */
[data-baseweb="tab-list"] {
  background: transparent !important;
  border-bottom: 1px solid var(--border) !important;
  gap: 4px !important;
}
[data-baseweb="tab"] {
  background: transparent !important;
  color: var(--text-muted) !important;
  font-family: 'Inter', sans-serif !important;
  font-weight: 500 !important;
  font-size: 0.83rem !important;
  padding: 8px 18px !important;
  border-radius: 8px 8px 0 0 !important;
  border: none !important;
  transition: color 0.18s ease, background 0.18s ease !important;
}
[data-baseweb="tab"]:hover {
  color: var(--text-primary) !important;
  background: rgba(255,255,255,0.03) !important;
}
[data-baseweb="tab"][aria-selected="true"] {
  color: var(--accent) !important;
  background: rgba(232,225,76,0.05) !important;
  text-shadow: 0 0 12px rgba(232,225,76,0.3) !important;
}
[data-baseweb="tab-highlight"] {
  background: linear-gradient(90deg, var(--accent), var(--accent-2)) !important;
  height: 2px !important;
  border-radius: 2px !important;
  transition: left 0.25s cubic-bezier(0.4,0,0.2,1) !important;
}
[data-baseweb="tab-border"] {
  background: var(--border) !important;
}

/* ── Primary buttons ── */
.stButton > button[kind="primary"],
[data-testid="stBaseButton-primary"] {
  background: linear-gradient(135deg, rgba(232,225,76,0.12), rgba(159,209,60,0.12)) !important;
  border: 1px solid rgba(232,225,76,0.35) !important;
  color: var(--accent) !important;
  font-family: 'Inter', sans-serif !important;
  font-weight: 600 !important;
  letter-spacing: 0.4px !important;
  border-radius: 10px !important;
  transition: all 0.2s ease !important;
}
.stButton > button[kind="primary"]:hover,
[data-testid="stBaseButton-primary"]:hover {
  background: linear-gradient(135deg, rgba(232,225,76,0.22), rgba(159,209,60,0.22)) !important;
  border-color: var(--accent) !important;
  box-shadow: 0 0 22px rgba(232,225,76,0.28) !important;
  transform: translateY(-1px) !important;
}
.stButton > button[kind="primary"]:active,
[data-testid="stBaseButton-primary"]:active {
  transform: scale(0.98) !important;
}

/* ── Secondary buttons ── */
.stButton > button[kind="secondary"],
[data-testid="stBaseButton-secondary"] {
  background: rgba(255,255,255,0.04) !important;
  border: 1px solid var(--border-hi) !important;
  color: var(--text-muted) !important;
  font-family: 'Inter', sans-serif !important;
  border-radius: 10px !important;
  transition: all 0.18s ease !important;
}
.stButton > button[kind="secondary"]:hover,
[data-testid="stBaseButton-secondary"]:hover {
  background: rgba(255,255,255,0.08) !important;
  color: var(--text-primary) !important;
  border-color: rgba(255,255,255,0.2) !important;
}

/* ── Inputs ── */
[data-baseweb="input"] {
  background: var(--bg-elevated) !important;
  border: 1px solid var(--border-hi) !important;
  border-radius: 10px !important;
  transition: border-color 0.18s ease, box-shadow 0.18s ease !important;
}
[data-baseweb="input"]:focus-within {
  border-color: var(--accent) !important;
  box-shadow: 0 0 0 3px rgba(232,225,76,0.1) !important;
}
[data-baseweb="input"] input {
  color: var(--text-primary) !important;
  background: transparent !important;
  font-family: 'Inter', sans-serif !important;
}
[data-baseweb="input"] input::placeholder {
  color: var(--text-muted) !important;
  opacity: 1 !important;
}
[data-testid="stNumberInput"] input {
  background: var(--bg-elevated) !important;
  color: var(--text-primary) !important;
  border-color: var(--border-hi) !important;
  border-radius: 10px !important;
}

/* ── Selectbox ── */
[data-baseweb="select"] > div:first-child {
  background: var(--bg-elevated) !important;
  border: 1px solid var(--border-hi) !important;
  border-radius: 10px !important;
  color: var(--text-primary) !important;
  transition: border-color 0.18s ease !important;
}
[data-baseweb="select"] > div:first-child:focus-within {
  border-color: var(--accent) !important;
  box-shadow: 0 0 0 3px rgba(232,225,76,0.1) !important;
}

/* ── Progress bar ── */
[data-testid="stProgress"] > div > div > div > div {
  background: linear-gradient(90deg, var(--accent), var(--accent-2)) !important;
  box-shadow: 0 0 10px rgba(232,225,76,0.5) !important;
  border-radius: 4px !important;
}

/* ── Expanders ── */
[data-testid="stExpander"] {
  border: 1px solid var(--border) !important;
  border-radius: 12px !important;
  background: var(--bg-panel) !important;
}

/* ── Code blocks ── */
code, .stCode code {
  font-family: 'JetBrains Mono', monospace !important;
  background: var(--bg-elevated) !important;
  color: var(--accent) !important;
  border: 1px solid var(--border) !important;
  border-radius: 6px !important;
  font-size: 0.78rem !important;
}

/* ── Dividers ── */
hr { border-color: var(--border) !important; margin: 10px 0 !important; }

/* ── Ready pill ── */
.ready-pill {
  display: inline-flex; align-items: center; gap: 8px;
  background: rgba(43,217,138,0.07);
  border: 1px solid rgba(43,217,138,0.22);
  border-radius: 20px; padding: 7px 16px;
  font-size: 0.82rem; font-weight: 500; color: #2BD98A;
  margin: 4px 0; animation: pill-glow 2.8s ease-in-out infinite;
}
.ready-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: #2BD98A; box-shadow: 0 0 5px #2BD98A;
  animation: dot-beat 2.8s ease-in-out infinite; flex-shrink: 0;
}
@keyframes pill-glow {
  0%,100% { box-shadow: 0 0 0 0 rgba(43,217,138,0); }
  50%      { box-shadow: 0 0 14px rgba(43,217,138,0.18); }
}
@keyframes dot-beat {
  0%,100% { transform: scale(1); opacity: 1; }
  50%      { transform: scale(1.35); opacity: 0.6; }
}

/* ── Logo ring ── */
.lc-ring {
  width: 38px; height: 38px; border-radius: 50%; flex-shrink: 0;
  background: radial-gradient(circle at 38% 38%, rgba(255,255,255,0.9) 8%, #E8E14C 40%, transparent 70%);
  border: 2px solid #E8E14C;
  box-shadow: 0 0 14px #E8E14C, inset 0 0 8px rgba(232,225,76,0.25);
  animation: ring-pulse 2.6s ease-in-out infinite alternate;
}
@keyframes ring-pulse {
  0%   { box-shadow: 0 0 10px #E8E14C; }
  100% { box-shadow: 0 0 24px #E8E14C, 0 0 40px rgba(232,225,76,0.18); }
}

/* ── Status dot in header ── */
.status-dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: #2BD98A; box-shadow: 0 0 6px #2BD98A;
  animation: sdot-beat 2s ease-in-out infinite; vertical-align: middle;
  margin-right: 5px;
}
.status-dot.running {
  background: #E8E14C; box-shadow: 0 0 6px #E8E14C;
  animation: sdot-beat 0.8s ease-in-out infinite;
}
@keyframes sdot-beat {
  0%,100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.45; transform: scale(1.5); }
}

/* ── Search row fade-in ── */
.search-row {
  opacity: 0; transform: translateY(5px);
  animation: row-in 0.28s ease forwards;
  padding: 3px 0;
}
@keyframes row-in { to { opacity:1; transform:translateY(0); } }

/* ── Sent history rows ── */
.hist-row { padding: 6px 0; border-bottom: 1px solid var(--border); }

/* ── Reduce motion ── */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
</style>
""", unsafe_allow_html=True)

# ── One-time session init ─────────────────────────────────────────────────────

if "cleanup_done" not in st.session_state:
    _cr = cleanup_stale_leads(retention_days=RETENTION_DAYS)
    st.session_state["cleanup_done"] = True
    st.session_state["cleanup_count"] = _cr["drafts"] + _cr["leads"]

state = hs.snapshot()

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        """
        <div style="display:flex;align-items:center;gap:12px;padding:10px 0 22px 0">
          <div class="lc-ring"></div>
          <div>
            <div style="font-family:'Space Grotesk',sans-serif;font-size:1.2rem;
                        font-weight:800;color:#E8EAF0;line-height:1.2;
                        text-shadow:0 0 8px rgba(232,225,76,0.35);">LeadCrew AI</div>
            <div style="font-size:0.66rem;color:#E8E14C;letter-spacing:2px;
                        text-transform:uppercase;margin-top:1px;">V2 · AGENT CORE</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.divider()

    sent_today = today_count()
    remaining  = DAILY_LIMIT - sent_today
    st.metric("Sent today", f"{sent_today} / {DAILY_LIMIT}")

    pct = min(100, sent_today / max(DAILY_LIMIT, 1) * 100)
    if remaining <= 0:
        st.error("Daily limit reached — no more sends today")
    else:
        st.markdown(
            f"""
            <div style="height:3px;background:rgba(255,255,255,0.06);border-radius:2px;margin:4px 0 6px;">
              <div style="height:100%;width:{pct:.0f}%;
                   background:linear-gradient(90deg,#E8E14C,#9FD13C);border-radius:2px;
                   box-shadow:0 0 8px rgba(232,225,76,0.4);"></div>
            </div>
            <div style="color:#6B7280;font-size:0.73rem;margin-bottom:4px;">{remaining} remaining</div>
            """,
            unsafe_allow_html=True,
        )

    st.divider()

    gap     = get_send_gap()
    next_ok = gap.get("next_ok_at")
    if next_ok:
        try:
            diff = (datetime.fromisoformat(next_ok) - datetime.now()).total_seconds()
            if diff > 0:
                st.warning(f"Next send in {int(diff)//60}m {int(diff)%60}s", icon=":material/schedule:")
            else:
                st.markdown(
                    '<div class="ready-pill"><div class="ready-dot"></div>Ready to send</div>',
                    unsafe_allow_html=True,
                )
        except Exception:
            st.markdown(
                '<div class="ready-pill"><div class="ready-dot"></div>Ready to send</div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            '<div class="ready-pill"><div class="ready-dot"></div>Ready to send</div>',
            unsafe_allow_html=True,
        )

    _cc = st.session_state.get("cleanup_count", 0)
    if _cc:
        st.caption(f":material/cleaning_services: Auto-cleaned {_cc} stale item(s) on load ({RETENTION_DAYS}d retention)")

    st.divider()
    st.caption("SMTP CONFIG")
    smtp_host  = os.getenv("SMTP_HOST", "")
    smtp_email = os.getenv("SMTP_EMAIL", "")
    st.code(f"{smtp_email or '(not set)'}\n{smtp_host or '(not set)'}", language=None)

    if st.button("Test SMTP connection"):
        smtp_port  = int(os.getenv("SMTP_PORT", "465"))
        s_password = os.getenv("SMTP_PASSWORD", "")
        from_name  = os.getenv("SMTP_FROM_NAME", "Hassnat")
        _ssl_env   = os.getenv("SMTP_SSL", "").lower()
        use_ssl    = True if _ssl_env == "true" else (False if _ssl_env == "false" else None)
        if not smtp_host or not smtp_email or not s_password:
            st.error("SMTP_HOST / SMTP_EMAIL / SMTP_PASSWORD not set in .env")
        else:
            with st.spinner("Testing…"):
                r = test_smtp_connection(smtp_host, smtp_port, smtp_email, s_password,
                                         from_name, use_ssl=use_ssl)
            if r.get("success"):
                st.success(
                    f"{r['mode']} · {r['host']}:{r['port']} — "
                    f"test email sent to {smtp_email}",
                    icon=":material/check_circle:",
                )
            else:
                st.error(f"{r.get('error','Unknown error')} "
                         f"[{r.get('mode','?')} · {r.get('host','?')}:{r.get('port','?')}]",
                         icon=":material/error:")

    if state["running"]:
        st.divider()
        st.warning("Harvest running…", icon=":material/schedule:")
        if st.button("Cancel / reset harvest", icon=":material/cancel:"):
            hs.reset()
            st.rerun()

    if st.button("Refresh", icon=":material/refresh:"):
        st.rerun()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _grade_badge(grade: str) -> str:
    color = {"A+": "#00c853", "A": "#2196f3"}.get(grade or "", "#9e9e9e")
    return (
        f'<span style="background:{color};color:#fff;padding:2px 9px;'
        f'border-radius:12px;font-size:0.7rem;font-weight:700;'
        f'letter-spacing:0.4px;vertical-align:middle">{_html.escape(grade or "?")}</span>'
    )


def _score_color(score: int) -> str:
    if score >= 80: return "#ff4b4b"
    if score >= 60: return "#ff9d00"
    return "#9e9e9e"


def _social_platform(social_links_json: str) -> str:
    try:
        links = json.loads(social_links_json or "[]")
        if not links:
            return ""
        url = links[0]
        if "instagram" in url:  return "Instagram"
        if "facebook"  in url:  return "Facebook"
        if "tiktok"    in url:  return "TikTok"
        if "linkedin"  in url:  return "LinkedIn"
        if "x.com"     in url or "twitter" in url: return "X/Twitter"
        return "social media"
    except Exception:
        return ""


def _wa_url(phone: str, name: str, rating: float, rating_count: int,
            social_platform: str = "") -> str:
    if not phone:
        return ""
    digits = phone.lstrip("+").replace(" ", "").replace("-", "")
    if not digits or not digits.isdigit():
        return ""
    if social_platform:
        msg = (
            f"Hi! I noticed that {name} has {rating:.1f} stars and {rating_count} reviews "
            f"on Google — great reputation! I also see you're active on {social_platform}. "
            f"While social media is great for engagement, customers searching on Google "
            f"won't find you without a proper website. "
            f"I can build you a free demo so you can see the difference. "
            f"Interested? — Hassnat, Haxantech"
        )
    else:
        msg = (
            f"Hi! I noticed that {name} has {rating:.1f} stars and {rating_count} reviews "
            f"on Google — great reputation! However, I couldn't find a website for your business. "
            f"These days customers search online and expect a website before visiting. "
            f"I can build you a free demo site so you can see how it looks. "
            f"Would you like to take a look? — Hassnat, Haxantech"
        )
    return (
        f"https://api.whatsapp.com/send?phone={digits}"
        f"&text={_urlquote(msg, safe='')}"
    )


def _issue_pills(issues_json: str, n: int = 3) -> str:
    try:
        issues = json.loads(issues_json or "[]")
    except Exception:
        return ""
    parts = []
    for item in issues[:n]:
        key   = item[0] if item else ""
        ev    = item[2] if len(item) > 2 else (item[1] if len(item) > 1 else "")
        label = _html.escape(key)
        desc  = _html.escape(ev[:40])
        parts.append(
            f'<span style="background:rgba(255,92,124,0.12);color:#FF5C7C;'
            f'padding:1px 8px;border-radius:8px;font-size:0.7rem;font-weight:600">'
            f'{label}</span>'
            f'<span style="color:#6B7280;font-size:0.78rem"> {desc}</span>'
        )
    return "&nbsp; ".join(parts)


def _send_button(draft_id: int, key: str) -> None:
    gap      = get_send_gap()
    next_ok  = gap.get("next_ok_at")
    disabled  = False
    btn_label = "APPROVE & SEND"
    btn_icon  = ":material/send:"

    if next_ok:
        try:
            diff = (datetime.fromisoformat(next_ok) - datetime.now()).total_seconds()
            if diff > 0:
                disabled  = True
                btn_label = f"{int(diff)//60}m {int(diff)%60}s"
                btn_icon  = ":material/schedule:"
        except Exception:
            pass

    if today_count() >= DAILY_LIMIT:
        st.error("Daily limit reached", icon=":material/error:")
        return

    if st.button(btn_label, key=key, disabled=disabled, type="primary", icon=btn_icon):
        with st.spinner("Sending…"):
            result = send_approved_draft(draft_id)
        if result.get("success"):
            st.success("Sent", icon=":material/check_circle:")
            st.rerun()
        elif result.get("wait_seconds"):
            wait = result["wait_seconds"]
            st.warning(f"Too soon — wait {wait//60}m {wait%60}s", icon=":material/schedule:")
        else:
            st.error(result.get("error", "Send failed"), icon=":material/error:")


# ── Page header ───────────────────────────────────────────────────────────────

_sent_now      = today_count()
_remaining_now = DAILY_LIMIT - _sent_now
_dot_cls       = "running" if state["running"] else ""
_status_label  = "Pipeline Running" if state["running"] else "Audit-First Pipeline"

col_brand, col_hstat1, col_hstat2 = st.columns([5, 1, 1])

with col_brand:
    st.markdown(
        f"""
        <div style="display:flex;align-items:center;gap:16px;padding:4px 0 8px;">
          <div class="lc-ring" style="width:44px;height:44px;"></div>
          <div>
            <div style="font-family:'Space Grotesk',sans-serif;font-size:1.65rem;
                        font-weight:800;color:#E8EAF0;line-height:1.15;
                        text-shadow:0 0 16px rgba(232,225,76,0.3);">
              LeadCrew AI
              <span style="font-size:0.88rem;color:#9FD13C;font-weight:500;margin-left:4px;">// DASHBOARD</span>
            </div>
            <div style="font-size:0.68rem;color:#E8E14C;letter-spacing:2.5px;text-transform:uppercase;margin-top:3px;">
              <span class="status-dot {_dot_cls}"></span>{_status_label} · V2
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with col_hstat1:
    st.metric("Sent Today", f"{_sent_now}")

with col_hstat2:
    st.metric("Remaining", f"{max(0, _remaining_now)}")

st.divider()


# ── Agent pipeline ────────────────────────────────────────────────────────────

STAGES = [
    ("SCOUT",     "Sourcing"),
    ("HUNTER",    "Contacts"),
    ("AUDITOR",   "Site Audit"),
    ("WRITER",    "Drafting"),
    ("SENDER",    "Delivery"),
    ("FOLLOW-UP", "Nurture"),
]


def _pipeline_state(log_lines: list, is_running: bool, is_done: bool) -> dict:
    if is_done:
        return {"active": -1, "completed": 5}
    if not is_running:
        return {"active": -1, "completed": -1}
    joined = " ".join(log_lines).lower()
    if "pipeline complete" in joined:
        return {"active": -1, "completed": 5}
    if "writing email" in joined:
        return {"active": 3, "completed": 2}
    if "enriching contacts" in joined:
        return {"active": 3, "completed": 1}
    if "scoring" in joined:
        return {"active": 2, "completed": 1}
    if "auditing" in joined:
        return {"active": 2, "completed": 0}
    if "new businesses" in joined or "harvesting" in joined:
        return {"active": 0, "completed": -1}
    return {"active": 0, "completed": -1}


def _core_html(ps: dict, running: bool, done: bool, error: str,
                sent_today: int, daily_limit: int, new_q: int,
                last_log: str) -> str:
    active, completed = ps["active"], ps["completed"]
    all_done = (active == -1 and completed == 5)

    if error:
        badge_cls, badge_txt = "bdg-err", "Failed"
    elif all_done:
        badge_cls, badge_txt = "bdg-done", "Done"
    elif running:
        badge_cls, badge_txt = "bdg-active", "Running"
    else:
        badge_cls, badge_txt = "bdg-idle", "Idle"

    bars, ticks = [], []
    for i, (code, role) in enumerate(STAGES):
        if all_done:
            st_cls = "done"
        elif i == active:
            st_cls = "active"
        elif completed >= i:
            st_cls = "done"
        else:
            st_cls = "idle"
        bars.append(
            f'<div class="bar-row"><span class="bar-lbl">{code}</span>'
            f'<div class="bar-track"><div class="bar-fill {st_cls}"></div></div></div>'
        )
        ticks.append(f'<div class="tick {st_cls}" title="{code} · {role}"></div>')

    if error:
        status_main, status_sub = "PIPELINE FAILED", _html.escape(error[:90])
    elif all_done:
        status_main = "PIPELINE COMPLETE"
        status_sub  = f"{new_q} new qualified lead(s) this search"
    elif running:
        role = STAGES[active][1] if 0 <= active < len(STAGES) else "Working"
        status_main, status_sub = "PIPELINE RUNNING", f"{role}…"
    else:
        status_main, status_sub = "AWAITING INPUT", "Fill in niche + city, then start the harvest"

    stage_label = (
        "ERROR" if error else
        ("DONE" if all_done else (STAGES[active][0] if running and 0 <= active < len(STAGES) else "IDLE"))
    )

    pct = max(0.0, min(1.0, sent_today / max(daily_limit, 1)))
    circ = 2 * 3.14159265 * 30
    dash_offset = circ * (1 - pct)

    last_log_safe = _html.escape((last_log or "").replace("**", "")[:64])

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
        "@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700;800&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');"
        "*{box-sizing:border-box;margin:0;padding:0;}"
        "html,body{background:#07090F;}"
        "body{font-family:'Inter',system-ui,sans-serif;height:100%;}"
        ".hud{position:relative;height:536px;border:1px solid rgba(255,255,255,0.07);border-radius:16px;"
        "background:radial-gradient(circle at 50% 38%,rgba(232,225,76,0.05),transparent 60%),"
        "linear-gradient(160deg,#0B0F18,#070910);overflow:hidden;display:flex;flex-direction:column;padding:18px 22px 16px;}"
        ".corner{position:absolute;width:20px;height:20px;}"
        ".tl{top:10px;left:10px;border-top:2px solid rgba(232,225,76,0.45);border-left:2px solid rgba(232,225,76,0.45);}"
        ".tr{top:10px;right:10px;border-top:2px solid rgba(232,225,76,0.45);border-right:2px solid rgba(232,225,76,0.45);}"
        ".bl{bottom:10px;left:10px;border-bottom:2px solid rgba(232,225,76,0.45);border-left:2px solid rgba(232,225,76,0.45);}"
        ".br{bottom:10px;right:10px;border-bottom:2px solid rgba(232,225,76,0.45);border-right:2px solid rgba(232,225,76,0.45);}"
        ".hud-top{display:flex;align-items:center;justify-content:space-between;flex-shrink:0;}"
        ".hud-label{font-size:0.68rem;font-weight:600;letter-spacing:2.5px;text-transform:uppercase;color:#4B5563;}"
        ".bdg{font-size:0.67rem;padding:3px 10px;border-radius:12px;font-weight:600;letter-spacing:0.4px;display:flex;align-items:center;gap:6px;}"
        ".bdg::before{content:'';width:6px;height:6px;border-radius:50%;background:currentColor;}"
        ".bdg-idle{background:rgba(107,114,128,0.12);color:#6B7280;border:1px solid rgba(107,114,128,0.18);}"
        ".bdg-active{background:rgba(232,225,76,0.1);color:#E8E14C;border:1px solid rgba(232,225,76,0.28);animation:bdg-blink 1s ease-in-out infinite;}"
        ".bdg-done{background:rgba(159,209,60,0.1);color:#9FD13C;border:1px solid rgba(159,209,60,0.28);}"
        ".bdg-err{background:rgba(255,92,124,0.1);color:#FF5C7C;border:1px solid rgba(255,92,124,0.28);}"
        "@keyframes bdg-blink{0%,100%{opacity:1;}50%{opacity:0.55;}}"
        ".hud-body{flex:1;display:flex;align-items:center;gap:8px;min-height:0;}"
        ".hud-left{width:172px;flex-shrink:0;display:flex;flex-direction:column;gap:10px;}"
        ".hl-cap{font-size:0.62rem;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:#4B5563;}"
        ".hl-stage{font-family:'Space Grotesk',sans-serif;font-size:1.3rem;font-weight:800;color:#E8EAF0;"
        "text-shadow:0 0 14px rgba(232,225,76,0.25);letter-spacing:0.5px;}"
        ".bar-row{display:flex;align-items:center;gap:8px;}"
        ".bar-lbl{font-family:'JetBrains Mono',monospace;font-size:0.52rem;color:#4B5563;width:54px;flex-shrink:0;letter-spacing:0.3px;}"
        ".bar-track{flex:1;height:4px;background:rgba(255,255,255,0.05);border-radius:2px;overflow:hidden;}"
        ".bar-fill{height:100%;width:6%;border-radius:2px;background:rgba(107,114,128,0.4);transition:width 0.5s ease,background 0.5s ease;}"
        ".bar-fill.active{width:62%;background:linear-gradient(90deg,#E8E14C,#9FD13C);box-shadow:0 0 8px rgba(232,225,76,0.5);animation:fill-pulse 1.2s ease-in-out infinite;}"
        ".bar-fill.done{width:100%;background:#9FD13C;}"
        "@keyframes fill-pulse{0%,100%{opacity:1;}50%{opacity:0.6;}}"
        ".hl-log{margin-top:auto;font-family:'JetBrains Mono',monospace;font-size:0.6rem;color:#6B7280;line-height:1.5;"
        "border-top:1px solid rgba(255,255,255,0.06);padding-top:8px;word-break:break-word;}"
        ".hud-orb-wrap{flex:1;height:100%;display:flex;align-items:center;justify-content:center;min-width:0;}"
        ".hud-right{width:104px;flex-shrink:0;display:flex;flex-direction:column;align-items:center;gap:8px;}"
        ".gauge-num{font-family:'JetBrains Mono',monospace;font-size:0.78rem;fill:#E8EAF0;}"
        ".gauge-cap{font-size:0.58rem;color:#4B5563;letter-spacing:1.5px;text-transform:uppercase;text-align:center;}"
        ".hud-bottom{flex-shrink:0;display:flex;align-items:center;gap:16px;padding-top:14px;}"
        ".hex{width:34px;height:30px;flex-shrink:0;background:linear-gradient(150deg,#141B2D,#0E1320);"
        "border:1px solid rgba(232,225,76,0.3);clip-path:polygon(25% 0%,75% 0%,100% 50%,75% 100%,25% 100%,0% 50%);"
        "box-shadow:0 0 10px rgba(232,225,76,0.1);display:flex;align-items:center;justify-content:center;}"
        ".hex-dot{width:6px;height:6px;border-radius:50%;background:#E8E14C;box-shadow:0 0 6px #E8E14C;}"
        ".hud-status{flex:1;min-width:0;}"
        ".hud-status-main{font-family:'Space Grotesk',sans-serif;font-size:0.92rem;font-weight:700;color:#E8EAF0;"
        "letter-spacing:1px;text-shadow:0 0 12px rgba(232,225,76,0.2);}"
        ".hud-status-sub{font-size:0.68rem;color:#6B7280;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}"
        ".swatch-row{display:flex;gap:5px;flex-shrink:0;}"
        ".tick{width:16px;height:7px;border-radius:3px;background:rgba(75,85,99,0.25);}"
        ".tick.active{background:#E8E14C;box-shadow:0 0 7px rgba(232,225,76,0.7);animation:bdg-blink 1s ease-in-out infinite;}"
        ".tick.done{background:#9FD13C;}"
        "@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;}}"
        "</style></head><body>"
        '<div class="hud">'
        '<div class="corner tl"></div><div class="corner tr"></div>'
        '<div class="corner bl"></div><div class="corner br"></div>'
        '<div class="hud-top"><span class="hud-label">Agent Core</span>'
        f'<span class="bdg {badge_cls}">{badge_txt}</span></div>'
        '<div class="hud-body">'
        '<div class="hud-left">'
        '<div class="hl-cap">Stage</div>'
        f'<div class="hl-stage">{stage_label}</div>'
        f'{"".join(bars)}'
        f'<div class="hl-log">{last_log_safe}</div>'
        '</div>'
        '<div class="hud-orb-wrap"><canvas id="orb"></canvas></div>'
        '<div class="hud-right">'
        '<svg width="76" height="76" viewBox="0 0 76 76">'
        '<circle cx="38" cy="38" r="30" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="5"/>'
        f'<circle cx="38" cy="38" r="30" fill="none" stroke="#E8E14C" stroke-width="5" '
        f'stroke-dasharray="{circ:.1f}" stroke-dashoffset="{dash_offset:.1f}" stroke-linecap="round" '
        'transform="rotate(-90 38 38)" style="filter:drop-shadow(0 0 4px rgba(232,225,76,0.6))"/>'
        f'<text x="38" y="42" text-anchor="middle" class="gauge-num">{sent_today}/{daily_limit}</text>'
        '</svg>'
        '<div class="gauge-cap">Sent Today</div>'
        '</div>'
        '</div>'
        '<div class="hud-bottom">'
        '<div class="hex"><div class="hex-dot"></div></div>'
        '<div class="hud-status">'
        f'<div class="hud-status-main">{status_main}</div>'
        f'<div class="hud-status-sub">{_html.escape(status_sub)}</div>'
        '</div>'
        f'<div class="swatch-row">{"".join(ticks)}</div>'
        '</div>'
        '</div>'
        "<script>"
        "var cv=document.getElementById('orb');"
        "var wrap=cv.parentElement;"
        "var dpr=window.devicePixelRatio||1;"
        "var size=Math.max(160,Math.min(wrap.clientWidth,wrap.clientHeight)-4);"
        "cv.style.width=size+'px';cv.style.height=size+'px';"
        "cv.width=size*dpr;cv.height=size*dpr;"
        "var ctx=cv.getContext('2d');ctx.scale(dpr,dpr);"
        "var cx=size/2,cy=size/2,R=size/2-12;"
        f"var running={'true' if running else 'false'};"
        f"var allDone={'true' if all_done else 'false'};"
        f"var hasErr={'true' if error else 'false'};"
        "function genBranches(){"
        "var br=[];"
        "function grow(x,y,ang,len,depth){"
        "if(depth>6||len<4)return;"
        "var x2=x+Math.cos(ang)*len,y2=y+Math.sin(ang)*len;"
        "if(Math.hypot(x2-cx,y2-cy)>R*0.9)return;"
        "br.push({x1:x,y1:y,x2:x2,y2:y2,w:Math.max(0.4,2.2-depth*0.28)});"
        "var kids=depth<2?3:(Math.random()<0.7?2:1);"
        "for(var i=0;i<kids;i++){"
        "grow(x2,y2,ang+(Math.random()-0.5)*1.1,len*(0.68+Math.random()*0.18),depth+1);"
        "}}"
        "var mains=9;"
        "for(var i=0;i<mains;i++){"
        "var a=(i/mains)*Math.PI*2+Math.random()*0.3;"
        "grow(cx,cy,a,R*0.16+Math.random()*8,0);"
        "}"
        "return br;}"
        "var branches=genBranches();"
        "var particles=[];"
        "for(var i=0;i<130;i++){"
        "particles.push({a:Math.random()*Math.PI*2,r:Math.random()*R*0.95,"
        "s:0.4+Math.random()*1.5,tw:Math.random()*Math.PI*2,sp:(Math.random()-0.5)*0.0025});"
        "}"
        "var glowColor=hasErr?'255,92,124':'232,225,76';"
        "var t=0,ringAngle=0;"
        "function draw(){"
        "ctx.clearRect(0,0,size,size);"
        "ctx.save();ctx.translate(cx,cy);ctx.rotate(ringAngle);"
        "ctx.strokeStyle='rgba('+glowColor+',0.22)';ctx.lineWidth=1;"
        "ctx.beginPath();ctx.arc(0,0,R,0,Math.PI*2);ctx.stroke();"
        "var ticks=48;"
        "for(var i=0;i<ticks;i++){"
        "var ang=i/ticks*Math.PI*2,major=i%4===0;"
        "var r1=R,r2=R-(major?10:5);"
        "ctx.strokeStyle=major?('rgba('+glowColor+',0.55)'):('rgba('+glowColor+',0.2)');"
        "ctx.lineWidth=major?1.4:0.8;"
        "ctx.beginPath();"
        "ctx.moveTo(Math.cos(ang)*r1,Math.sin(ang)*r1);"
        "ctx.lineTo(Math.cos(ang)*r2,Math.sin(ang)*r2);"
        "ctx.stroke();}"
        "ctx.restore();"
        "ctx.save();ctx.translate(cx,cy);"
        "var pulse=0.92+Math.sin(t*0.05)*0.06+(running?Math.sin(t*0.22)*0.05:0);"
        "ctx.scale(pulse,pulse);"
        "ctx.shadowBlur=26;ctx.shadowColor='rgba('+glowColor+',0.85)';"
        "ctx.strokeStyle='rgba(255,250,225,0.9)';"
        "branches.forEach(function(b){"
        "ctx.lineWidth=b.w;ctx.beginPath();"
        "ctx.moveTo(b.x1-cx,b.y1-cy);ctx.lineTo(b.x2-cx,b.y2-cy);ctx.stroke();});"
        "var grd=ctx.createRadialGradient(0,0,0,0,0,24);"
        "grd.addColorStop(0,'rgba(255,255,255,0.95)');"
        "grd.addColorStop(0.5,'rgba('+glowColor+',0.55)');"
        "grd.addColorStop(1,'rgba('+glowColor+',0)');"
        "ctx.shadowBlur=0;ctx.fillStyle=grd;"
        "ctx.beginPath();ctx.arc(0,0,24,0,Math.PI*2);ctx.fill();"
        "ctx.restore();"
        "ctx.save();ctx.translate(cx,cy);"
        "particles.forEach(function(p){"
        "p.a+=p.sp*(running?2.2:1);"
        "var tw=0.5+0.5*Math.sin(t*0.04+p.tw);"
        "var x=Math.cos(p.a)*p.r,y=Math.sin(p.a)*p.r;"
        "ctx.fillStyle='rgba('+glowColor+','+(0.15+tw*0.5)+')';"
        "ctx.beginPath();ctx.arc(x,y,p.s,0,Math.PI*2);ctx.fill();});"
        "ctx.restore();"
        "ringAngle+=running?0.006:0.0022;"
        "t++;requestAnimationFrame(draw);}"
        "draw();"
        "</script>"
        "</body></html>"
    )


def _terminal_html(log_lines: list) -> str:
    log_json = json.dumps(log_lines[-30:])
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
        "@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap');"
        "*{box-sizing:border-box;margin:0;padding:0;}"
        "body{background:#07090F;font-family:'JetBrains Mono','Courier New',monospace;}"
        ".terminal{background:rgba(10,15,26,0.92);border:1px solid rgba(255,255,255,0.08);"
        "border-top:1px solid rgba(255,255,255,0.13);border-radius:12px;overflow:hidden;}"
        ".bar{display:flex;align-items:center;gap:6px;padding:8px 14px;"
        "background:rgba(255,255,255,0.025);border-bottom:1px solid rgba(255,255,255,0.06);}"
        ".dot{width:10px;height:10px;border-radius:50%;}"
        ".dr{background:#FF5C7C;}.dy{background:#F5A524;}.dg{background:#2BD98A;}"
        ".lbl{font-size:0.63rem;color:#4B5563;margin-left:8px;letter-spacing:1.5px;text-transform:uppercase;}"
        ".body{padding:12px 14px 10px;max-height:230px;overflow-y:auto;"
        "display:flex;flex-direction:column;gap:2px;}"
        ".body::-webkit-scrollbar{width:3px;}"
        ".body::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.08);border-radius:2px;}"
        ".ln{font-size:0.72rem;color:#9CA3AF;line-height:1.55;"
        "opacity:0;transform:translateY(5px);animation:lnin 0.25s ease forwards;"
        "white-space:pre-wrap;word-break:break-word;}"
        ".ln strong{color:#E8EAF0;}"
        ".suc{color:#2BD98A;}.err{color:#FF5C7C;}.wrn{color:#F5A524;}.inf{color:#E8E14C;}"
        "@keyframes lnin{to{opacity:1;transform:translateY(0);}}"
        "@media(prefers-reduced-motion:reduce){.ln{animation:none;opacity:1;transform:none;}}"
        "</style></head><body>"
        "<div class='terminal'>"
        "<div class='bar'><div class='dot dr'></div><div class='dot dy'></div><div class='dot dg'></div>"
        "<span class='lbl'>Agent Log</span></div>"
        "<div class='body' id='tb'></div></div>"
        "<script>"
        f"var lines={log_json};"
        "function cls(l){"
        "if(l.indexOf('complete')>=0||l.indexOf('Done')>=0)return 'suc';"
        "if(l.indexOf('Error')>=0||l.indexOf('error')>=0||l.indexOf('fail')>=0)return 'err';"
        "if(l.indexOf('Warning')>=0)return 'wrn';"
        "return 'inf';}"
        "function md(t){return t.replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>');}"
        "var tb=document.getElementById('tb');"
        "lines.forEach(function(l,i){"
        "var d=document.createElement('div');"
        "d.className='ln '+cls(l);"
        "d.style.animationDelay=(i*0.033)+'s';"
        "d.innerHTML=md(l);"
        "tb.appendChild(d);});"
        "setTimeout(function(){tb.scrollTop=tb.scrollHeight;},lines.length*33+130);"
        "</script></body></html>"
    )


# ── Main tabs ─────────────────────────────────────────────────────────────────

tab_search, tab_pending, tab_followups, tab_wa, tab_history = st.tabs([
    "New Search",
    "Website Issues",
    "Follow-ups",
    "No Website Leads",
    "Sent History",
], on_change="rerun")

# Only the active tab's body runs each rerun (lazy tabs) — with auto-rerun
# polling every 1.5s during a live harvest, running all 5 tabs' DB queries
# and HTML rendering on every tick was the main source of UI lag.

# ── Tab 0: New Search ─────────────────────────────────────────────────────────

def _render_tab_search():

    # Hero — Agent Core
    ps      = _pipeline_state(state["log"], state["running"], state["done"])
    new_q_s = state["stats"].get("new_qualified", 0) if state["done"] else 0
    last_log_line = state["log"][-1] if state["log"] else ""
    components.html(
        _core_html(ps, state["running"], state["done"], state["error"],
                   _sent_now, DAILY_LIMIT, new_q_s, last_log_line),
        height=556, scrolling=False,
    )

    st.divider()
    st.markdown(
        '<div style="font-size:0.75rem;font-weight:600;color:#6B7280;'
        'letter-spacing:1.5px;text-transform:uppercase;margin-bottom:10px;">'
        'New Lead Harvest</div>',
        unsafe_allow_html=True,
    )

    col_n, col_c, col_co, col_t = st.columns([3, 3, 2, 1])
    with col_n:
        niche_input = st.text_input("Niche",
            placeholder="dentists, car dealerships, piercing studios",
            key="search_niche", disabled=state["running"])
    with col_c:
        city_input = st.text_input("City",
            placeholder="Lahore, Manchester, Rome",
            key="search_city", disabled=state["running"])
    with col_co:
        country_input = st.selectbox("Country",
            options=list(COUNTRY_OPTIONS.keys()),
            key="search_country", disabled=state["running"])
    with col_t:
        target_input = st.number_input("Target leads",
            min_value=1, max_value=150, value=20, step=5,
            key="search_target", disabled=state["running"],
            help="How many new qualified leads to aim for. If the city/niche runs dry, "
                 "the search automatically retries with a few similar niche names before "
                 "giving up. Also caps how many leads get enriched + drafted.")

    country_cfg  = COUNTRY_OPTIONS[country_input]
    market_label = "WhatsApp" if country_cfg["market"] == "whatsapp" else "Email"
    city_preview = (city_input or "<city>") + country_cfg["suffix"]
    st.caption(f"Channel: **{market_label}** · Serper query: **{city_preview}**")

    btn_ready    = bool(niche_input.strip() and city_input.strip())
    btn_disabled = state["running"] or not btn_ready

    if state["running"]:
        st.info("Pipeline is running — live feed below…", icon=":material/schedule:")
    elif not btn_ready:
        st.caption("Fill in Niche and City to enable the button.")

    if st.button("Start Harvest", disabled=btn_disabled, type="primary",
                 icon=":material/rocket_launch:", use_container_width=True):
        hs.start()
        city_clean  = city_input.strip()
        city_serper = city_clean + country_cfg["suffix"]
        t = threading.Thread(
            target=_run_pipeline,
            args=(niche_input.strip(), city_clean, city_serper,
                  country_input, country_cfg["market"], int(target_input)),
            daemon=True,
        )
        t.start()
        time.sleep(0.3)
        st.rerun()

    st.divider()

    # Progress / result banner
    if state["running"] or state["done"] or state["log"]:
        if not state["running"]:
            if state["error"]:
                st.error(f"Pipeline failed: {state['error']}", icon=":material/error:")
                if st.button("Clear error"):
                    hs.reset()
                    st.rerun()
            else:
                s       = state["stats"]
                new_q   = s.get("new_qualified", 0)
                total_q = s.get("total_qualified", 0)
                harv    = s.get("harvested", 0)
                if new_q > 0:
                    st.success(
                        f"Done — **{new_q} new qualified leads** · "
                        f"{total_q} total in DB · leads with email → **Website Issues** · "
                        f"no email → **No Website Leads**",
                        icon=":material/check_circle:",
                    )
                else:
                    st.info(
                        f"No new leads this search — all {harv if harv else 'found'} businesses "
                        f"were already in your database. Try a different city or niche.  "
                        f"({total_q} total qualified leads already in DB)",
                        icon=":material/info:",
                    )
                if st.button("Start another search", icon=":material/refresh:"):
                    hs.reset()
                    st.rerun()

        # Terminal log (replaces expander)
        if state["log"]:
            components.html(_terminal_html(state["log"]), height=300, scrolling=False)

    # Recent searches
    search_hist = get_search_history(10)
    if search_hist:
        st.divider()
        st.markdown(
            '<div style="font-size:0.68rem;font-weight:600;color:#6B7280;'
            'letter-spacing:1.5px;text-transform:uppercase;margin-bottom:8px;">'
            'Recent Searches</div>',
            unsafe_allow_html=True,
        )
        for idx, row in enumerate(search_hist):
            ran   = row["ran_at"][:16].replace("T", " ")
            chan  = "WhatsApp" if row["market"] == "whatsapp" else "Email"
            st.markdown(
                f'<div class="search-row" style="animation-delay:{idx * 0.055}s">'
                f'<span style="color:#6B7280;font-size:0.68rem;text-transform:uppercase;'
                f'letter-spacing:0.5px;">{chan}</span> '
                f'<strong style="color:#E8EAF0">{_html.escape(row["niche"])}</strong>'
                f'<span style="color:#6B7280"> · {_html.escape(row["city"])}, '
                f'{_html.escape(row["country"])} · {row["leads_found"]} leads · {ran}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )


with tab_search:
    if tab_search.open:
        _render_tab_search()


# ── Tab 1: Website Issues ─────────────────────────────────────────────────────

def _render_tab_pending():
    draft_dates = get_draft_dates()
    if draft_dates:
        with st.expander("Delete drafts by date", icon=":material/delete:"):
            date_labels = [f"{d['draft_date']}  ({d['cnt']} drafts)" for d in draft_dates]
            sel_label   = st.selectbox("Select date", options=date_labels, key="del_date_sel")
            sel_idx     = date_labels.index(sel_label)
            sel_date    = draft_dates[sel_idx]["draft_date"]
            sel_count   = draft_dates[sel_idx]["cnt"]
            st.warning(
                f"Permanently deletes **{sel_count} draft(s)** generated on **{sel_date}**. "
                "The businesses stay in the database (dedup still works)."
            )
            if st.button(f"Delete {sel_count} draft(s) from {sel_date}",
                         key="del_date_btn", type="secondary", icon=":material/delete:"):
                deleted = delete_drafts_by_date(sel_date)
                st.success(f"Deleted {deleted} draft(s) from {sel_date}.", icon=":material/check_circle:")
                st.rerun()
        st.divider()

    leads    = get_leads_with_drafts(status="draft")
    sendable = [r for r in leads if r.get("email")]

    if not leads:
        st.info("No pending drafts. Run a **New Search** to harvest leads and generate drafts.")
    else:
        if "auto_send_queue" in st.session_state:
            queue     = st.session_state["auto_send_queue"]
            total     = st.session_state.get("auto_send_total", 1)
            log_lines = st.session_state.get("auto_send_sent_log", [])
            done      = total - len(queue)

            if queue:
                st.progress(done / total, text=f"Auto-sending: {done} / {total}")

                gap_info  = get_send_gap()
                next_ok   = gap_info.get("next_ok_at")
                remaining = 0
                if next_ok:
                    try:
                        remaining = max(
                            0,
                            (datetime.fromisoformat(next_ok) - datetime.now()).total_seconds()
                        )
                    except Exception:
                        remaining = 0

                if remaining > 0:
                    countdown = st.empty()
                    wait = min(int(remaining), 5)
                    for i in range(wait):
                        countdown.info(f"Next send in {int(remaining - i)}s…", icon=":material/schedule:")
                        time.sleep(1)
                    st.rerun()
                    st.stop()
                else:
                    draft_id = queue[0]
                    result   = send_approved_draft(draft_id)
                    if result.get("success"):
                        log_lines = log_lines + [f"Sent draft #{draft_id}"]
                    elif result.get("wait_seconds"):
                        log_lines = log_lines + [f"Gap hit — will retry #{draft_id}"]
                        st.session_state["auto_send_sent_log"] = log_lines
                        st.rerun()
                        st.stop()
                    else:
                        log_lines = log_lines + [
                            f"Skipped #{draft_id}: {result.get('error', 'skipped')}"
                        ]
                    st.session_state["auto_send_queue"]    = queue[1:]
                    st.session_state["auto_send_sent_log"] = log_lines
                    st.rerun()
                    st.stop()

            else:
                st.success(f"Done! {total} email(s) processed.", icon=":material/celebration:")
                for line in log_lines:
                    st.caption(line)
                if st.button("Clear & review", key="auto_send_clear"):
                    for k in ("auto_send_queue", "auto_send_total", "auto_send_sent_log"):
                        st.session_state.pop(k, None)
                    st.rerun()

        else:
            col_cap, col_btn = st.columns([4, 1])
            with col_cap:
                no_email_count = len(leads) - len(sendable)
                note = f" · {no_email_count} missing email" if no_email_count else ""
                st.caption(f"{len(leads)} draft(s) — {len(sendable)} ready to send{note}")
            with col_btn:
                if sendable:
                    if st.button(f"Send All ({len(sendable)})", icon=":material/send:",
                                 type="primary", key="auto_send_start"):
                        st.session_state["auto_send_queue"]    = [r["draft_id"] for r in sendable]
                        st.session_state["auto_send_total"]    = len(sendable)
                        st.session_state["auto_send_sent_log"] = []
                        st.rerun()

        st.divider()

        for row in leads:
            name     = row["name"] or ""
            city     = row["city"] or ""
            grade    = row["grade"] or ""
            score    = row["pain_score"] or 0
            rating   = row["rating"] or 0.0
            rc       = row["rating_count"] or 0
            email    = row["email"] or ""
            draft_id = row["draft_id"]
            subject  = row["subject"] or ""
            body     = row["body"] or ""
            is_nw    = bool(row.get("no_website"))

            with st.container(border=True):
                col_info, col_btn = st.columns([5, 1])
                with col_info:
                    nw_tag = " · no website" if is_nw else ""
                    sc     = _score_color(score)
                    st.markdown(
                        f'{_grade_badge(grade)}&nbsp; '
                        f'<strong style="color:#E8EAF0">{_html.escape(name)}</strong> '
                        f'<span style="color:#6B7280">· {_html.escape(city)}{nw_tag}</span> '
                        f'<span style="color:{sc};font-size:0.8rem">· {score}/100</span>',
                        unsafe_allow_html=True,
                    )
                    st.caption(
                        f"★ {rating:.1f}  ({rc} reviews)  |  "
                        f"{(':material/mail: ' + email) if email else ':material/warning: no email'}"
                    )
                    pills = _issue_pills(row.get("issues_json", ""))
                    if pills:
                        st.markdown(pills, unsafe_allow_html=True)
                with col_btn:
                    if not email:
                        st.button("No email", key=f"noemail_{draft_id}",
                                  disabled=True, help="No email found — appears in WhatsApp tab")
                    else:
                        _send_button(draft_id, key=f"send_{draft_id}")

                with st.expander(subject[:70], icon=":material/description:"):
                    st.text(f"To: {email or '(none)'}")
                    st.text(f"Subject: {subject}")
                    st.divider()
                    st.text(body)
                    if st.button("Discard", key=f"discard_{draft_id}", icon=":material/delete:"):
                        update_draft_status(draft_id, "discarded")
                        st.rerun()


with tab_pending:
    if tab_pending.open:
        _render_tab_pending()


# ── Tab 2: Follow-ups ─────────────────────────────────────────────────────────

def _render_tab_followups():
    all_fu = get_all_followups()

    if not all_fu:
        st.info("No follow-ups yet — they appear 3 days after each send.")
    else:
        pending_fu = [f for f in all_fu if f["status"] == "pending"]
        drafted_fu = [f for f in all_fu if f["status"] == "drafted"]
        done_fu    = [f for f in all_fu if f["status"] not in ("pending", "drafted")]

        if pending_fu:
            st.subheader(f"Due for follow-up ({len(pending_fu)})", anchor=False)
            if st.button("Generate all due follow-up drafts", type="primary",
                         icon=":material/auto_awesome:"):
                api_key = os.getenv("OPENAI_API_KEY", "")
                if not api_key:
                    st.error("OPENAI_API_KEY not set in .env", icon=":material/error:")
                else:
                    from write_emails import write_followup_drafts
                    with st.spinner("Generating…"):
                        n = write_followup_drafts(api_key=api_key)
                    st.success(f"Created {n} follow-up draft(s)", icon=":material/check_circle:")
                    st.rerun()
            for fu in pending_fu:
                st.markdown(
                    f"- **{fu['name']}** · {fu['city']} · "
                    f"*{fu['original_subject'][:50]}* · due {fu.get('due_at','')[:10]}"
                )

        if drafted_fu:
            st.subheader(f"Follow-up drafts ready ({len(drafted_fu)})", anchor=False)
            for fu in drafted_fu:
                st.markdown(
                    f"- **{fu['name']}** · draft #{fu['followup_draft_id']} · "
                    f"*{fu['original_subject'][:50]}*"
                )

        if done_fu:
            st.subheader(f"Completed ({len(done_fu)})", anchor=False)
            for fu in done_fu:
                st.caption(f"{fu['name']} — {fu['status']}")


with tab_followups:
    if tab_followups.open:
        _render_tab_followups()


# ── Tab 3: No Website Leads ───────────────────────────────────────────────────

def _render_no_website_leads(leads: list[dict], social: bool) -> None:
    if not leads:
        msg = "No social-only leads yet." if social else "No pure no-website leads yet."
        st.info(msg)
        return

    for lead in leads:
        phone    = lead.get("phone") or ""
        name     = lead.get("name") or ""
        rating   = float(lead.get("rating") or 0)
        rc       = int(lead.get("rating_count") or 0)
        grade    = lead.get("grade") or ""
        mkt      = lead.get("market") or ""
        sl       = lead.get("social_links") or ""
        platform = _social_platform(sl) if social else ""
        try:
            social_url = (json.loads(sl) or [""])[0]
        except Exception:
            social_url = ""
        wa_link = _wa_url(phone, name, rating, rc, social_platform=platform)

        with st.container(border=True):
            col_l, col_r = st.columns([4, 1])
            with col_l:
                tag     = " · PK" if mkt == "whatsapp" else ""
                p_score = lead.get("pain_score", 0) or 0
                sc      = _score_color(p_score)
                st.markdown(
                    f'{_grade_badge(grade)}&nbsp; '
                    f'<strong style="color:#E8EAF0">{_html.escape(name)}</strong> '
                    f'<span style="color:#6B7280">· {_html.escape(lead.get("city", "") or "")}{tag}</span> '
                    f'<span style="color:{sc};font-size:0.8rem">· {p_score}/100</span>',
                    unsafe_allow_html=True,
                )
                st.caption(f"★ {rating:.1f}  ({rc} reviews)")
                if phone:
                    st.caption(f":material/call: {phone}")
                if social_url:
                    st.caption(f":material/link: {social_url[:60]}")
            with col_r:
                if wa_link:
                    st.link_button("WhatsApp", wa_link, icon=":material/chat:")
                else:
                    st.caption("No phone")


def _render_tab_wa():
    all_nw_leads = get_no_website_leads_full()

    if not all_nw_leads:
        st.info("No qualified no-website leads yet. Run a search to populate this tab.")
    else:
        nw_leads = [l for l in all_nw_leads if not _social_platform(l.get("social_links") or "")]
        so_leads = [l for l in all_nw_leads if     _social_platform(l.get("social_links") or "")]

        st.caption(
            f"{len(all_nw_leads)} total — "
            f"{len(nw_leads)} no website · {len(so_leads)} social only"
        )

        _fields = ["grade", "pain_score", "name", "phone", "wa_link", "social",
                   "rating", "rating_count", "city", "address", "niche", "category"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_fields, extrasaction="ignore")
        writer.writeheader()
        for lead in all_nw_leads:
            ph  = lead.get("phone") or ""
            pl  = _social_platform(lead.get("social_links") or "")
            lnk = _wa_url(ph, lead.get("name") or "",
                          float(lead.get("rating") or 0),
                          int(lead.get("rating_count") or 0),
                          social_platform=pl)
            try:
                soc = (json.loads(lead.get("social_links") or "[]") or [""])[0]
            except Exception:
                soc = ""
            writer.writerow({**lead, "wa_link": lnk, "social": soc})

        st.download_button(
            "Download CSV (all)",
            data=buf.getvalue().encode("utf-8"),
            file_name=f"no_website_leads_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            icon=":material/download:",
        )
        st.divider()

        sub_nw, sub_so = st.tabs([
            f"No Website ({len(nw_leads)})",
            f"Social Only ({len(so_leads)})",
        ])

        with sub_nw:
            st.caption("Pitch: business has zero web presence — completely invisible on Google.")
            _render_no_website_leads(nw_leads, social=False)

        with sub_so:
            st.caption("Pitch: only presence is Instagram/Facebook — Google can't index social pages.")
            _render_no_website_leads(so_leads, social=True)


with tab_wa:
    if tab_wa.open:
        _render_tab_wa()


# ── Tab 4: Sent history ───────────────────────────────────────────────────────

def _render_tab_history():
    history = recent_sent(limit=50)
    if not history:
        st.info("Nothing sent yet.")
    else:
        st.caption(f"Last {len(history)} sends")
        h1, h2, h3 = st.columns([3, 3, 2])
        with h1:
            st.markdown(
                '<span style="color:#6B7280;font-size:0.7rem;font-weight:600;'
                'letter-spacing:1.2px;text-transform:uppercase;">Company</span>',
                unsafe_allow_html=True,
            )
        with h2:
            st.markdown(
                '<span style="color:#6B7280;font-size:0.7rem;font-weight:600;'
                'letter-spacing:1.2px;text-transform:uppercase;">Email</span>',
                unsafe_allow_html=True,
            )
        with h3:
            st.markdown(
                '<span style="color:#6B7280;font-size:0.7rem;font-weight:600;'
                'letter-spacing:1.2px;text-transform:uppercase;">Sent at</span>',
                unsafe_allow_html=True,
            )
        st.divider()
        for row in history:
            sent_at = row.get("sent_at", "")[:16].replace("T", " ")
            c1, c2, c3 = st.columns([3, 3, 2])
            with c1:
                st.markdown(
                    f'<span style="color:#E8EAF0;font-weight:600;font-size:0.85rem;">'
                    f'{_html.escape(row.get("company",""))}</span>',
                    unsafe_allow_html=True,
                )
            with c2:
                st.caption(row.get("to_email", ""))
            with c3:
                st.caption(sent_at)


with tab_history:
    if tab_history.open:
        _render_tab_history()


# ── Auto-rerun when pipeline is running ──────────────────────────────────────
if state["running"]:
    time.sleep(1.5)
    st.rerun()
