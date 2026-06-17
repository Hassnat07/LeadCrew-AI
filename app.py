import streamlit as st
import os
import re
import html as _html_mod
import time
from datetime import datetime

# ── Auto-load .env file ───────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Load Email Environment Variables ──────────────────────────────────────────
env_gmail = os.getenv("GMAIL_ADDRESS")
env_gpass = os.getenv("GMAIL_APP_PASSWORD")

# ── Load User/Company Environment Variables ───────────────────────────────────
env_name = os.getenv("YOUR_NAME")
env_company = os.getenv("YOUR_COMPANY")

# ── Load API Keys from Environment ─────────────────────────────────────────────
env_openai_key = os.getenv("OPENAI_API_KEY")
env_serper_key = os.getenv("SERPER_API_KEY")
env_pagespeed_key = os.getenv("PAGESPEED_API_KEY", "")

# ── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Omnix — Autonomous Lead Generation",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@300;400;500;600&display=swap');

:root {
    --bg: #05070a;
    --surface: #0a0f1a;
    --surface2: #111827;
    --surface3: #1a2332;
    --border: #1e293b;
    --border-glow: #334155;
    --accent: #06b6d4;
    --accent2: #8b5cf6;
    --accent3: #10b981;
    --accent-glow: rgba(6,182,212,0.3);
    --text: #f1f5f9;
    --text-dim: #94a3b8;
    --text-muted: #64748b;
    --success: #10b981;
    --warning: #f59e0b;
    --danger: #ef4444;
}

html, body, .stApp {
    background-color: var(--bg) !important;
    color: var(--text) !important;
    font-family: 'Inter', sans-serif !important;
}

/* ===== SCROLLBAR ===== */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: var(--surface); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--border-glow); }

/* ===== SIDEBAR ===== */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, var(--surface) 0%, #0d1117 100%) !important;
    border-right: 1px solid var(--border) !important;
}
section[data-testid="stSidebar"] * {
    color: var(--text) !important;
}

/* ===== HEADERS ===== */
h1, h2, h3 {
    font-family: 'Inter', sans-serif !important;
    color: var(--text) !important;
    letter-spacing: -0.02em !important;
}

/* ===== INPUTS ===== */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea,
.stSelectbox > div > div {
    background: var(--surface2) !important;
    border: 1px solid var(--border) !important;
    color: var(--text) !important;
    border-radius: 10px !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 13px !important;
    transition: all 0.2s ease !important;
}
.stTextInput > div > div > input:focus,
.stTextArea > div > div > textarea:focus,
.stSelectbox > div > div:focus-within {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px rgba(6,182,212,0.15) !important;
}

/* Labels */
label, .stTextInput label, .stTextArea label, .stSelectbox label {
    color: var(--text-muted) !important;
    font-size: 11px !important;
    text-transform: uppercase !important;
    letter-spacing: 0.12em !important;
    font-family: 'Inter', sans-serif !important;
    font-weight: 600 !important;
}

/* ===== BUTTONS ===== */
.stButton > button {
    background: linear-gradient(135deg, var(--accent2), #6366f1) !important;
    color: white !important;
    border: none !important;
    border-radius: 10px !important;
    font-family: 'Inter', sans-serif !important;
    font-weight: 700 !important;
    font-size: 14px !important;
    letter-spacing: 0.02em !important;
    padding: 14px 32px !important;
    transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
    width: 100% !important;
    position: relative !important;
    overflow: hidden !important;
}
.stButton > button:hover {
    transform: translateY(-2px) !important;
    box-shadow: 0 12px 40px rgba(139,92,246,0.4) !important;
}
.stButton > button:active {
    transform: translateY(0) !important;
}
.stButton > button:disabled {
    opacity: 0.5 !important;
    cursor: not-allowed !important;
}

/* Secondary button style */
button[kind="secondary"] {
    background: var(--surface2) !important;
    border: 1px solid var(--border) !important;
    color: var(--text-dim) !important;
}
button[kind="secondary"]:hover {
    background: var(--surface3) !important;
    border-color: var(--border-glow) !important;
}

/* ===== EXPANDER ===== */
.streamlit-expanderHeader {
    background: linear-gradient(135deg, var(--surface2), var(--surface3)) !important;
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
    color: var(--text) !important;
    font-family: 'Inter', sans-serif !important;
    font-weight: 600 !important;
    font-size: 14px !important;
}
.streamlit-expanderContent {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    border-top: none !important;
    border-radius: 0 0 12px 12px !important;
}

/* ===== METRICS ===== */
[data-testid="metric-container"] {
    background: linear-gradient(135deg, var(--surface2), var(--surface3)) !important;
    border: 1px solid var(--border) !important;
    border-radius: 14px !important;
    padding: 20px !important;
    transition: all 0.3s ease !important;
}
[data-testid="metric-container"]:hover {
    border-color: var(--border-glow) !important;
    transform: translateY(-2px) !important;
    box-shadow: 0 8px 30px rgba(0,0,0,0.3) !important;
}
[data-testid="metric-container"] label {
    color: var(--text-muted) !important;
    font-size: 11px !important;
    text-transform: uppercase !important;
    letter-spacing: 0.1em !important;
}
[data-testid="metric-container"] [data-testid="stMetricValue"] {
    color: var(--accent) !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 32px !important;
    font-weight: 800 !important;
}

/* ===== CODE / TERMINAL ===== */
code, pre {
    background: var(--surface2) !important;
    border: 1px solid var(--border) !important;
    color: var(--accent) !important;
    font-family: 'JetBrains Mono', monospace !important;
    border-radius: 8px !important;
    font-size: 12px !important;
}

/* ===== ALERTS ===== */
.stSuccess {
    background: rgba(16,185,129,0.08) !important;
    border: 1px solid rgba(16,185,129,0.25) !important;
    color: var(--text) !important;
    border-radius: 10px !important;
}
.stInfo {
    background: rgba(6,182,212,0.06) !important;
    border: 1px solid rgba(6,182,212,0.2) !important;
    color: var(--text) !important;
    border-radius: 10px !important;
}
.stWarning {
    background: rgba(245,158,11,0.08) !important;
    border: 1px solid rgba(245,158,11,0.25) !important;
    color: var(--text) !important;
    border-radius: 10px !important;
}
.stError {
    background: rgba(239,68,68,0.08) !important;
    border: 1px solid rgba(239,68,68,0.25) !important;
    border-radius: 10px !important;
}

/* ===== DIVIDER ===== */
hr {
    border-color: var(--border) !important;
    margin: 28px 0 !important;
}

/* ===== PROGRESS BAR ===== */
.stProgress > div > div {
    background: linear-gradient(90deg, var(--accent), var(--accent2)) !important;
    border-radius: 4px !important;
    transition: width 0.5s ease !important;
}

/* ===== ANIMATIONS ===== */
@keyframes pulse-glow {
    0%, 100% { box-shadow: 0 0 5px var(--accent-glow), 0 0 10px var(--accent-glow); }
    50% { box-shadow: 0 0 15px var(--accent-glow), 0 0 30px var(--accent-glow); }
}
@keyframes typing-dot {
    0%, 60%, 100% { transform: translateY(0); }
    30% { transform: translateY(-6px); }
}
@keyframes status-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}
@keyframes slide-in {
    from { opacity: 0; transform: translateY(10px); }
    to { opacity: 1; transform: translateY(0); }
}
@keyframes border-flow {
    0% { background-position: 0% 50%; }
    50% { background-position: 100% 50%; }
    100% { background-position: 0% 50%; }
}
@keyframes shimmer {
    0% { background-position: -200% 0; }
    100% { background-position: 200% 0; }
}

/* ===== ARC REACTOR ANIMATIONS ===== */
.arc-reactor-container {
    filter: drop-shadow(0 0 20px rgba(6,182,212,0.5));
}
@keyframes spin-slow { 100% { transform: rotate(360deg); } }
@keyframes spin-reverse { 100% { transform: rotate(-360deg); } }
@keyframes core-pulse { 
    0% { transform: scale(0.95); opacity: 0.8; }
    100% { transform: scale(1.05); opacity: 1; filter: brightness(1.2); }
}

/* ===== HOLOGRAM SPINNER FOR PROCESSING ===== */
.hologram-spinner {
    width: 60px;
    height: 60px;
    border-radius: 50%;
    border: 3px solid transparent;
    border-top-color: var(--accent);
    border-bottom-color: var(--accent2);
    animation: spin-reverse 1.5s linear infinite;
    position: relative;
    filter: drop-shadow(0 0 10px var(--accent));
}
.hologram-spinner::after {
    content: '';
    position: absolute;
    inset: 5px;
    border-radius: 50%;
    border: 2px solid transparent;
    border-left-color: var(--accent3);
    border-right-color: var(--accent);
    animation: spin-slow 1s linear infinite;
}
.hologram-spinner.researcher { border-top-color: #06b6d4; border-bottom-color: #3b82f6; }
.hologram-spinner.analyst { border-top-color: #8b5cf6; border-bottom-color: #d946ef; }
.hologram-spinner.writer { border-top-color: #10b981; border-bottom-color: #059669; }
.hologram-spinner.system { border-top-color: #f59e0b; border-bottom-color: #d97706; }

/* ===== AGENT CARDS ===== */
.agent-card {
    background: rgba(10, 15, 26, 0.7);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(6, 182, 212, 0.3);
    border-radius: 16px;
    padding: 20px;
    text-align: center;
    position: relative;
    overflow: hidden;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    box-shadow: inset 0 0 20px rgba(6, 182, 212, 0.05), 0 4px 15px rgba(0, 0, 0, 0.5);
}
.agent-card::after {
    content: '';
    position: absolute;
    inset: 0;
    background-image: radial-gradient(rgba(6, 182, 212, 0.15) 1px, transparent 1px);
    background-size: 15px 15px;
    opacity: 0.4;
    pointer-events: none;
}
.agent-card:hover {
    transform: translateY(-5px);
    border-color: rgba(6, 182, 212, 0.8);
    box-shadow: 0 15px 40px rgba(6, 182, 212, 0.2), inset 0 0 40px rgba(6, 182, 212, 0.15);
}
.agent-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0; height: 3px;
    background: linear-gradient(90deg, transparent, var(--accent), var(--accent2), transparent);
    opacity: 0;
    transition: opacity 0.3s ease;
}
.agent-card:hover::before {
    opacity: 1;
    animation: scanning-laser 2s infinite linear;
}
@keyframes scanning-laser {
    0% { transform: translateY(-100%); }
    50% { transform: translateY(10000%); }
    100% { transform: translateY(-100%); }
}

.agent-avatar {
    width: 64px;
    height: 64px;
    border-radius: 50%;
    border: 2px solid rgba(6, 182, 212, 0.4);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 28px;
    margin: 0 auto 12px;
    position: relative;
    box-shadow: 0 0 15px rgba(6, 182, 212, 0.2);
}
.agent-avatar::before {
    content: '';
    position: absolute;
    inset: -4px;
    border-radius: 50%;
    border: 1px dashed rgba(6, 182, 212, 0.6);
    animation: spin-slow 10s linear infinite;
}

.agent-status {
    position: absolute;
    bottom: -2px; right: -2px;
    width: 14px; height: 14px;
    border-radius: 50%;
    border: 2px solid var(--surface2);
}
.agent-status.idle { background: var(--text-muted); }
.agent-status.running { background: var(--accent); animation: pulse-glow 1.5s infinite; }
.agent-status.complete { background: var(--success); }
.agent-status.error { background: var(--danger); }

.agent-name {
    font-family: 'Inter', sans-serif;
    font-weight: 700;
    font-size: 15px;
    color: var(--text);
    margin-bottom: 4px;
}
.agent-role {
    font-size: 11px;
    color: var(--accent);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 6px;
}
.agent-desc {
    font-size: 12px;
    color: var(--text-muted);
    line-height: 1.5;
}

/* ===== PIPELINE ARROW ===== */
.pipeline-arrow {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--border-glow);
    font-size: 20px;
    position: relative;
}
.pipeline-arrow::after {
    content: '';
    position: absolute;
    width: 100%;
    height: 2px;
    background: linear-gradient(90deg, var(--border), var(--border-glow), var(--border));
    top: 50%;
    transform: translateY(-50%);
    z-index: 0;
}
.pipeline-arrow span {
    background: var(--bg);
    padding: 0 8px;
    z-index: 1;
    font-size: 18px;
}

/* ===== LIVE LOG TERMINAL ===== */
.terminal-window {
    background: rgba(2, 4, 8, 0.85);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(6, 182, 212, 0.4);
    box-shadow: 0 0 30px rgba(6, 182, 212, 0.1), inset 0 0 20px rgba(6, 182, 212, 0.05);
    border-radius: 14px;
    overflow: hidden;
    font-family: 'JetBrains Mono', monospace;
    position: relative;
}
.terminal-window::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
    background: linear-gradient(rgba(6, 182, 212, 0.05) 50%, transparent 50%);
    background-size: 100% 4px;
    pointer-events: none;
    z-index: 10;
}
.terminal-header {
    background: rgba(6, 182, 212, 0.1);
    padding: 10px 16px;
    display: flex;
    align-items: center;
    gap: 8px;
    border-bottom: 1px solid rgba(6, 182, 212, 0.3);
}
.terminal-dot { width: 10px; height: 10px; border-radius: 50%; }
.terminal-dot.red { background: #ff5f56; }
.terminal-dot.yellow { background: #ffbd2e; }
.terminal-dot.green { background: #27ca40; }
.terminal-title {
    font-size: 11px;
    color: #06b6d4;
    text-shadow: 0 0 5px rgba(6, 182, 212, 0.5);
    margin-left: 4px;
    font-weight: 500;
}
.terminal-body {
    padding: 16px;
    font-size: 12px;
    line-height: 2;
    color: var(--text-dim);
    max-height: 400px;
    overflow-y: auto;
}
.terminal-line {
    animation: slide-in 0.3s ease forwards;
    opacity: 0;
}
.terminal-line .timestamp {
    color: var(--text-muted);
    font-size: 10px;
    margin-right: 8px;
}
.terminal-line .agent-tag {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-right: 8px;
}
.terminal-line .agent-tag.researcher { background: rgba(6,182,212,0.15); color: #22d3ee; }
.terminal-line .agent-tag.analyst { background: rgba(139,92,246,0.15); color: #a78bfa; }
.terminal-line .agent-tag.writer { background: rgba(16,185,129,0.15); color: #34d399; }
.terminal-line .agent-tag.system { background: rgba(245,158,11,0.15); color: #fbbf24; }

/* ===== TYPING INDICATOR ===== */
.typing-indicator {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 8px 12px;
    background: var(--surface2);
    border-radius: 20px;
    width: fit-content;
    margin: 8px 0;
}
.typing-indicator span {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: var(--accent);
    animation: typing-dot 1.4s infinite ease-in-out both;
}
.typing-indicator span:nth-child(1) { animation-delay: -0.32s; }
.typing-indicator span:nth-child(2) { animation-delay: -0.16s; }

/* ===== RESULT CARDS ===== */
.result-card {
    background: linear-gradient(135deg, var(--surface2), var(--surface3));
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 24px;
    margin: 12px 0;
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    line-height: 1.8;
    white-space: pre-wrap;
    color: var(--text-dim);
    position: relative;
    overflow: hidden;
}
.result-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0;
    width: 3px; height: 100%;
    background: linear-gradient(180deg, var(--accent), var(--accent2));
}

/* ===== BADGES ===== */
.agent-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 11px;
    font-family: 'Inter', sans-serif;
    font-weight: 700;
    letter-spacing: 0.03em;
}
.badge-researcher { background: rgba(6,182,212,0.12); color: #22d3ee; border: 1px solid rgba(6,182,212,0.25); }
.badge-analyst { background: rgba(139,92,246,0.12); color: #a78bfa; border: 1px solid rgba(139,92,246,0.25); }
.badge-writer { background: rgba(16,185,129,0.12); color: #34d399; border: 1px solid rgba(16,185,129,0.25); }

/* ===== HERO SECTION ===== */
.hero-section {
    position: relative;
    padding: 8px 0 24px;
}
.hero-tag {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(6,182,212,0.08);
    border: 1px solid rgba(6,182,212,0.2);
    color: var(--accent);
    font-size: 11px;
    font-family: 'JetBrains Mono', monospace;
    padding: 5px 14px;
    border-radius: 20px;
    letter-spacing: 0.08em;
    margin-bottom: 16px;
}
.hero-tag .pulse {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: var(--accent);
    animation: status-pulse 2s infinite;
}

/* ===== LIVE STATUS BAR ===== */
.live-status-bar {
    background: linear-gradient(90deg, var(--surface2), var(--surface3));
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 20px;
    display: flex;
    align-items: center;
    gap: 16px;
    margin: 16px 0;
}
.status-indicator {
    display: flex;
    align-items: center;
    gap: 8px;
}
.status-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    position: relative;
}
.status-dot.active {
    background: var(--success);
    box-shadow: 0 0 8px rgba(16,185,129,0.5);
}
.status-dot.active::after {
    content: '';
    position: absolute;
    inset: -3px;
    border-radius: 50%;
    border: 1px solid rgba(16,185,129,0.4);
    animation: pulse-glow 2s infinite;
}
.status-dot.idle { background: var(--text-muted); }
.status-text {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-dim);
    font-family: 'JetBrains Mono', monospace;
}

/* ===== CONTACT CARDS ===== */
.contact-card {
    background: linear-gradient(135deg, rgba(16,185,129,0.06), rgba(16,185,129,0.02));
    border: 1px solid rgba(16,185,129,0.15);
    border-radius: 12px;
    padding: 14px 18px;
    margin: 8px 0;
    transition: all 0.2s ease;
}
.contact-card:hover {
    border-color: rgba(16,185,129,0.3);
    transform: translateX(4px);
}

/* ===== DOWNLOAD BUTTON ===== */
.stDownloadButton > button {
    background: var(--surface2) !important;
    border: 1px solid var(--border) !important;
    color: var(--text-dim) !important;
    border-radius: 10px !important;
    font-family: 'Inter', sans-serif !important;
    font-weight: 600 !important;
    font-size: 13px !important;
    padding: 12px 24px !important;
    transition: all 0.2s ease !important;
}
.stDownloadButton > button:hover {
    background: var(--surface3) !important;
    border-color: var(--accent) !important;
    color: var(--accent) !important;
    transform: translateY(-1px);
}

/* ===== FOOTER CARD ===== */
.footer-card {
    background: linear-gradient(135deg, var(--surface2), var(--surface3));
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 40px;
    text-align: center;
    position: relative;
    overflow: hidden;
}
.footer-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--accent), transparent);
}
.footer-icon {
    font-size: 48px;
    margin-bottom: 16px;
    display: inline-block;
    animation: pulse-glow 3s infinite;
}

/* ===== SECTION HEADERS ===== */
.section-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 16px;
}
.section-header .icon {
    font-size: 20px;
}
.section-header h2 {
    font-size: 20px !important;
    font-weight: 700 !important;
    margin: 0 !important;
}

/* ===== CAMPAIGN FORM ===== */
.form-section {
    background: linear-gradient(135deg, var(--surface2), var(--surface3));
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 24px;
    margin-bottom: 20px;
}

/* ===== PRESET BUTTONS ===== */
.preset-btn {
    background: var(--surface2) !important;
    border: 1px solid var(--border) !important;
    color: var(--text-dim) !important;
    border-radius: 10px !important;
    font-size: 12px !important;
    padding: 10px 14px !important;
    transition: all 0.2s ease !important;
}
.preset-btn:hover {
    border-color: var(--accent) !important;
    color: var(--accent) !important;
    background: rgba(6,182,212,0.05) !important;
}

/* ===== STAT PILLS ===== */
.stat-pill {
    background: linear-gradient(135deg, var(--surface2), var(--surface3));
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px 16px;
    text-align: center;
    transition: all 0.3s ease;
    position: relative;
    overflow: hidden;
}
.stat-pill:hover {
    transform: translateY(-3px);
    border-color: var(--border-glow);
    box-shadow: 0 10px 30px rgba(0,0,0,0.35);
}
.stat-value {
    font-size: 30px;
    font-weight: 900;
    font-family: 'Inter', sans-serif;
    letter-spacing: -0.03em;
    line-height: 1;
    margin-bottom: 5px;
}
.stat-label {
    font-size: 10px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.12em;
}

/* ===== CAPABILITY CARDS ===== */
.capability-card {
    background: linear-gradient(135deg, var(--surface2), var(--surface3));
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 22px 18px;
    height: 100%;
    transition: all 0.3s ease;
    position: relative;
    overflow: hidden;
}
.capability-card:hover {
    transform: translateY(-4px);
    border-color: var(--border-glow);
    box-shadow: 0 14px 40px rgba(0,0,0,0.4);
}
.capability-card::after {
    content: '';
    position: absolute;
    bottom: 0; left: 0; right: 0; height: 2px;
    opacity: 0;
    transition: opacity 0.3s ease;
}
.capability-card.cyan::after  { background: linear-gradient(90deg, transparent, var(--accent), transparent); }
.capability-card.purple::after { background: linear-gradient(90deg, transparent, var(--accent2), transparent); }
.capability-card.green::after  { background: linear-gradient(90deg, transparent, var(--accent3), transparent); }
.capability-card:hover::after { opacity: 1; }
.cap-icon-wrap {
    width: 48px; height: 48px;
    border-radius: 14px;
    display: flex; align-items: center; justify-content: center;
    font-size: 24px;
    margin-bottom: 14px;
}
.cap-title {
    font-size: 14px;
    font-weight: 700;
    color: var(--text);
    margin-bottom: 6px;
    font-family: 'Inter', sans-serif;
}
.cap-desc {
    font-size: 12px;
    color: var(--text-muted);
    line-height: 1.6;
    margin-bottom: 14px;
}
.cap-tags { display: flex; gap: 6px; flex-wrap: wrap; }
.cap-tag {
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 10px;
    font-weight: 700;
    font-family: 'JetBrains Mono', monospace;
    letter-spacing: 0.03em;
}

/* ===== ANIMATED FUNNEL CHART ===== */
.funnel-chart {
    background: linear-gradient(135deg, var(--surface2), var(--surface3));
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px 24px;
}
.funnel-row {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
}
.funnel-label {
    font-size: 11px;
    font-family: 'JetBrains Mono', monospace;
    color: var(--text-muted);
    width: 90px;
    flex-shrink: 0;
    text-align: right;
}
.funnel-bar-wrap {
    flex: 1;
    height: 10px;
    background: var(--surface);
    border-radius: 5px;
    overflow: hidden;
}
.funnel-bar-fill {
    height: 100%;
    border-radius: 5px;
    animation: funnel-grow 1.8s cubic-bezier(0.4,0,0.2,1) forwards;
    transform-origin: left;
    transform: scaleX(0);
}
@keyframes funnel-grow {
    0%   { transform: scaleX(0); }
    60%  { transform: scaleX(1.03); }
    100% { transform: scaleX(1); }
}
.funnel-value {
    font-size: 11px;
    font-family: 'JetBrains Mono', monospace;
    font-weight: 700;
    width: 36px;
    flex-shrink: 0;
}

/* ===== HOW IT WORKS STEPS ===== */
.how-step {
    background: linear-gradient(135deg, var(--surface2), var(--surface3));
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px 16px;
    text-align: center;
    height: 100%;
    transition: all 0.3s ease;
}
.how-step:hover {
    transform: translateY(-3px);
    border-color: var(--border-glow);
}
.how-num {
    width: 32px; height: 32px;
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 13px; font-weight: 800;
    margin: 0 auto 10px;
    font-family: 'Inter', sans-serif;
}
.how-title { font-size: 13px; font-weight: 700; color: var(--text); margin-bottom: 5px; }
.how-desc  { font-size: 11px; color: var(--text-muted); line-height: 1.5; }
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="hero-section">
    <div style="display:flex;justify-content:center;margin-bottom:28px;padding-top:12px;">
        <div class="arc-reactor-container">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="120" height="120">
              <defs>
                <radialGradient id="core-glow" cx="50%" cy="50%" r="50%">
                  <stop offset="0%" stop-color="#ffffff" stop-opacity="1" />
                  <stop offset="20%" stop-color="#a78bfa" stop-opacity="0.9" />
                  <stop offset="50%" stop-color="#06b6d4" stop-opacity="0.6" />
                  <stop offset="100%" stop-color="#06b6d4" stop-opacity="0" />
                </radialGradient>
                <linearGradient id="ring-grad" x1="0%" y1="0%" x2="100%" y2="100%">
                  <stop offset="0%" stop-color="#06b6d4" />
                  <stop offset="50%" stop-color="#3b82f6" />
                  <stop offset="100%" stop-color="#8b5cf6" />
                </linearGradient>
              </defs>
              <g style="transform-origin: 50px 50px; animation: spin-slow 12s linear infinite;">
                  <circle cx="50" cy="50" r="44" fill="none" stroke="url(#ring-grad)" stroke-width="3" stroke-dasharray="15 8" opacity="0.8" />
              </g>
              <g style="transform-origin: 50px 50px; animation: spin-reverse 8s linear infinite;">
                  <circle cx="50" cy="50" r="36" fill="none" stroke="url(#ring-grad)" stroke-width="2" stroke-dasharray="6 4 12 4" opacity="0.9" />
              </g>
              <circle cx="50" cy="50" r="26" fill="url(#core-glow)" style="transform-origin: 50px 50px; animation: core-pulse 2s alternate infinite;" />
              <circle cx="50" cy="50" r="10" fill="#ffffff" opacity="0.9" />
              
              <path d="M50 6 L52 14 L48 14 Z" fill="#06b6d4" />
              <path d="M50 94 L52 86 L48 86 Z" fill="#06b6d4" />
              <path d="M6 50 L14 48 L14 52 Z" fill="#06b6d4" />
              <path d="M94 50 L86 48 L86 52 Z" fill="#06b6d4" />
              <path d="M19 19 L25 25 L21 27 Z" fill="#8b5cf6" />
              <path d="M81 81 L75 75 L79 73 Z" fill="#8b5cf6" />
              <path d="M81 19 L75 25 L73 21 Z" fill="#8b5cf6" />
              <path d="M19 81 L25 75 L27 79 Z" fill="#8b5cf6" />
            </svg>
        </div>
    </div>
    <div class="hero-tag"><span class="pulse"></span> LIVE MULTI-AGENT SYSTEM</div>
    <h1 style="font-size:44px;font-weight:900;margin-bottom:6px;letter-spacing:-0.03em;">
        <span style="background:linear-gradient(90deg,#06b6d4,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Omnix</span>
    </h1>
    <p style="color:#64748b;font-size:14px;margin-bottom:4px;font-weight:400;">
        Three autonomous AI agents working in sequence to find, research, and reach out to your ideal clients
    </p>
</div>
""", unsafe_allow_html=True)

# ── Agent Pipeline Visual ─────────────────────────────────────────────────────
st.markdown("<h3 style='font-size:14px;color:#64748b;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:16px;'>Agent Pipeline</h3>", unsafe_allow_html=True)

col1, col2, col3, col4, col5 = st.columns([2.2, 0.4, 2.2, 0.4, 2.2])

with col1:
    st.markdown("""
    <div class="agent-card">
        <div class="agent-avatar researcher">
            <div class="agent-status idle" id="status-1"></div>
        </div>
        <div class="agent-role">Agent 1</div>
        <div class="agent-name">Lead Researcher</div>
        <div class="agent-desc">Discovers qualified prospects with verified contact details</div>
    </div>
    """, unsafe_allow_html=True)

with col2:
    st.markdown("""
    <div class="pipeline-arrow" style="padding-top:40px;">
        <span>→</span>
    </div>
    """, unsafe_allow_html=True)

with col3:
    st.markdown("""
    <div class="agent-card">
        <div class="agent-avatar analyst">
            <div class="agent-status idle" id="status-2"></div>
        </div>
        <div class="agent-role">Agent 2</div>
        <div class="agent-name">Company Analyst</div>
        <div class="agent-desc">Deep-dives into each company to extract intel & pain points</div>
    </div>
    """, unsafe_allow_html=True)

with col4:
    st.markdown("""
    <div class="pipeline-arrow" style="padding-top:40px;">
        <span>→</span>
    </div>
    """, unsafe_allow_html=True)

with col5:
    st.markdown("""
    <div class="agent-card">
        <div class="agent-avatar writer">
            <div class="agent-status idle" id="status-3"></div>
        </div>
        <div class="agent-role">Agent 3</div>
        <div class="agent-name">Email Copywriter</div>
        <div class="agent-desc">Crafts personalized cold emails that actually get replies</div>
    </div>
    """, unsafe_allow_html=True)

st.markdown("<hr>", unsafe_allow_html=True)

# ── Sidebar: API Keys ─────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("<h2 style='font-size:18px;font-weight:800;margin-bottom:4px;'>Configuration</h2>", unsafe_allow_html=True)
    st.markdown("<p style='color:#64748b;font-size:12px;margin-bottom:20px;'>Add your API keys to power the agents</p>", unsafe_allow_html=True)

    openai_key = st.text_input(
        "OpenAI API Key",
        value=env_openai_key or "",
        type="password",
        placeholder="sk-...",
        help="Required for agent reasoning"
    )
    serper_key = st.text_input(
        "Serper API Key",
        value=env_serper_key or "",
        type="password",
        placeholder="Get free key at serper.dev",
        help="Required for web search"
    )
    pagespeed_key = st.text_input(
        "PageSpeed API Key (optional)",
        value=env_pagespeed_key or "",
        type="password",
        placeholder="Google Cloud API key",
        help="Raises audit limit from 25/day to 25,000/day. Free at console.cloud.google.com"
    )

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown("<p style='color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:8px;font-weight:600;'>Email Setup</p>", unsafe_allow_html=True)
    from email_sender import SMTP_PRESETS, test_smtp_connection
    smtp_choice = st.selectbox("Email Provider", list(SMTP_PRESETS.keys()), index=0, key="smtp_choice")
    preset = SMTP_PRESETS[smtp_choice]
    gmail_sidebar = st.text_input("Sender Email", value=env_gmail or "", placeholder="you@yourdomain.com", key="gmail_sidebar")
    gpass_sidebar = st.text_input("Email Password", value=env_gpass or "", type="password", placeholder=preset["note"], key="gpass_sidebar")
    if smtp_choice == "Custom":
        smtp_host_sidebar = st.text_input(
            "SMTP / IMAP Host",
            placeholder="mail.yourdomain.com",
            key="smtp_host_custom",
        )
        imap_host_sidebar = smtp_host_sidebar
        imap_port_sidebar = 993
    else:
        smtp_host_sidebar = preset["host"]
        imap_host_sidebar = preset["imap_host"]
        imap_port_sidebar = preset["imap_port"]
    smtp_port_sidebar = preset["port"]
    st.markdown(
        f"<div style='font-size:11px;color:#64748b;margin-top:4px;font-family:JetBrains Mono,monospace;'>"
        f"SMTP: <b style='color:#94a3b8;'>{smtp_host_sidebar}:{smtp_port_sidebar}</b><br>"
        f"IMAP: <b style='color:#94a3b8;'>{imap_host_sidebar}:{imap_port_sidebar}</b></div>",
        unsafe_allow_html=True
    )

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("Send Test Email to Myself", key="test_email_btn"):
        if not gmail_sidebar or not gpass_sidebar:
            st.error("Enter your email and password first.")
        else:
            _test_name = env_name or "Omnix"
            _test_company = env_company or "Omnix"
            with st.spinner("Sending test email..."):
                test_result = test_smtp_connection(
                    smtp_host_sidebar, smtp_port_sidebar,
                    gmail_sidebar, gpass_sidebar,
                    sender_name=_test_name,
                    sender_company=_test_company,
                )
            if test_result["success"]:
                st.success(f"Test email sent to {gmail_sidebar}")
            else:
                st.error(f"Failed: {test_result['error']}")

    # ── Daily send stats ──────────────────────────────────────────────────────
    st.markdown("<hr>", unsafe_allow_html=True)
    try:
        from database import today_count, total_count
        _today = today_count()
        _total = total_count()
        _remaining = max(0, 150 - _today)
        st.markdown(
            f"<p style='font-size:11px;color:#64748b;text-transform:uppercase;"
            f"letter-spacing:0.1em;margin-bottom:6px;font-weight:600;'>Daily Progress</p>",
            unsafe_allow_html=True,
        )
        st.progress(_today / 150, text=f"{_today}/150 sent today")
        st.markdown(
            f"<div style='font-size:11px;color:#64748b;font-family:JetBrains Mono,monospace;'>"
            f"Remaining today: <b style='color:#10b981;'>{_remaining}</b> &nbsp;|&nbsp; "
            f"All-time: <b style='color:#94a3b8;'>{_total}</b></div>",
            unsafe_allow_html=True,
        )
    except Exception:
        pass


# ── Platform Stats ────────────────────────────────────────────────────────────
st.markdown("<h3 style='font-size:13px;color:#64748b;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:14px;'>Platform Overview</h3>", unsafe_allow_html=True)

s1, s2, s3, s4 = st.columns(4)
with s1:
    st.markdown("""<div class="stat-pill">
        <div class="stat-value" style="color:#06b6d4;">3</div>
        <div class="stat-label">AI Agents</div>
    </div>""", unsafe_allow_html=True)
with s2:
    st.markdown("""<div class="stat-pill">
        <div class="stat-value" style="color:#8b5cf6;">~$0.10</div>
        <div class="stat-label">Per Lead</div>
    </div>""", unsafe_allow_html=True)
with s3:
    st.markdown("""<div class="stat-pill">
        <div class="stat-value" style="color:#10b981;">100%</div>
        <div class="stat-label">Automated</div>
    </div>""", unsafe_allow_html=True)
with s4:
    st.markdown("""<div class="stat-pill">
        <div class="stat-value" style="color:#f59e0b;">50+</div>
        <div class="stat-label">Industries</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── Capability Cards + Funnel Chart ───────────────────────────────────────────
cap1, cap2, cap3, chart_col = st.columns([1.1, 1.1, 1.1, 1.4])

with cap1:
    st.markdown("""
    <div class="capability-card cyan">
        <div class="cap-icon-wrap" style="background:rgba(6,182,212,0.12);"><span style="font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:800;color:#22d3ee;letter-spacing:-0.04em;">01</span></div>
        <div class="cap-title">Autonomous Research</div>
        <div class="cap-desc">AI scours the web to find verified contacts matching your exact target profile and location.</div>
        <div class="cap-tags">
            <span class="cap-tag" style="background:rgba(6,182,212,0.1);border:1px solid rgba(6,182,212,0.25);color:#22d3ee;">SerperDev</span>
            <span class="cap-tag" style="background:rgba(6,182,212,0.1);border:1px solid rgba(6,182,212,0.25);color:#22d3ee;">Web Scraper</span>
        </div>
    </div>""", unsafe_allow_html=True)

with cap2:
    st.markdown("""
    <div class="capability-card purple">
        <div class="cap-icon-wrap" style="background:rgba(139,92,246,0.12);"><span style="font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:800;color:#a78bfa;letter-spacing:-0.04em;">02</span></div>
        <div class="cap-title">Deep Company Intel</div>
        <div class="cap-desc">Extracts pain points, tech stack, and growth signals from each company's digital presence.</div>
        <div class="cap-tags">
            <span class="cap-tag" style="background:rgba(139,92,246,0.1);border:1px solid rgba(139,92,246,0.25);color:#a78bfa;">GPT-4o</span>
            <span class="cap-tag" style="background:rgba(139,92,246,0.1);border:1px solid rgba(139,92,246,0.25);color:#a78bfa;">NLP</span>
        </div>
    </div>""", unsafe_allow_html=True)

with cap3:
    st.markdown("""
    <div class="capability-card green">
        <div class="cap-icon-wrap" style="background:rgba(16,185,129,0.12);"><span style="font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:800;color:#34d399;letter-spacing:-0.04em;">03</span></div>
        <div class="cap-title">Hyper-Personalized Emails</div>
        <div class="cap-desc">Generates context-aware cold emails referencing each company's specific challenges and goals.</div>
        <div class="cap-tags">
            <span class="cap-tag" style="background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.25);color:#34d399;">CrewAI</span>
            <span class="cap-tag" style="background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.25);color:#34d399;">SMTP</span>
        </div>
    </div>""", unsafe_allow_html=True)

with chart_col:
    st.markdown("""
    <div class="funnel-chart">
        <div style="font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:16px;">Pipeline Funnel</div>
        <div class="funnel-row">
            <div class="funnel-label">Prospects</div>
            <div class="funnel-bar-wrap">
                <div class="funnel-bar-fill" style="width:100%;background:linear-gradient(90deg,#06b6d4,#0891b2);animation-delay:0s;"></div>
            </div>
            <div class="funnel-value" style="color:#22d3ee;">100%</div>
        </div>
        <div class="funnel-row">
            <div class="funnel-label">Researched</div>
            <div class="funnel-bar-wrap">
                <div class="funnel-bar-fill" style="width:85%;background:linear-gradient(90deg,#8b5cf6,#7c3aed);animation-delay:0.2s;"></div>
            </div>
            <div class="funnel-value" style="color:#a78bfa;">85%</div>
        </div>
        <div class="funnel-row">
            <div class="funnel-label">Analyzed</div>
            <div class="funnel-bar-wrap">
                <div class="funnel-bar-fill" style="width:72%;background:linear-gradient(90deg,#8b5cf6,#6d28d9);animation-delay:0.4s;"></div>
            </div>
            <div class="funnel-value" style="color:#a78bfa;">72%</div>
        </div>
        <div class="funnel-row">
            <div class="funnel-label">Emailed</div>
            <div class="funnel-bar-wrap">
                <div class="funnel-bar-fill" style="width:60%;background:linear-gradient(90deg,#10b981,#059669);animation-delay:0.6s;"></div>
            </div>
            <div class="funnel-value" style="color:#34d399;">60%</div>
        </div>
        <div class="funnel-row" style="margin-bottom:0;">
            <div class="funnel-label">Replied</div>
            <div class="funnel-bar-wrap">
                <div class="funnel-bar-fill" style="width:18%;background:linear-gradient(90deg,#f59e0b,#d97706);animation-delay:0.8s;"></div>
            </div>
            <div class="funnel-value" style="color:#fbbf24;">18%</div>
        </div>
        <div style="font-size:10px;color:#475569;margin-top:14px;font-family:'JetBrains Mono',monospace;">avg. cold email benchmark</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<hr>", unsafe_allow_html=True)

# ── Main Form ─────────────────────────────────────────────────────────────────
st.markdown("<div class='section-header'><h2>Campaign Setup</h2></div>", unsafe_allow_html=True)

col_a, col_b = st.columns(2)
with col_a:
    your_name = st.text_input("Your Name", value=env_name, placeholder="e.g. Ahmed Khan")
    company_name = st.text_input("Your Company / Freelancer Name", value=env_company, placeholder="e.g. KhanTech Solutions")

with col_b:
    target_industry = st.text_input(
        "Target Industry",
        placeholder="e.g. SaaS startups, e-commerce brands, real estate agencies"
    )
    your_service = st.text_input(
        "Your Service / Offer",
        placeholder="e.g. AI automation workflows, web development, SEO services"
    )

col_c, col_d = st.columns(2)
with col_c:
    COUNTRIES = [
        "Worldwide (No restriction)",
        "United States", "United Kingdom", "Canada", "Australia",
        "Pakistan", "India", "UAE", "Saudi Arabia", "Germany",
        "France", "Netherlands", "Singapore", "South Africa",
        "Nigeria", "Philippines", "Brazil", "Mexico",
        "New Zealand", "Ireland", "Sweden", "Denmark", "Norway",
        "Other (type below)",
    ]
    target_country = st.selectbox(
        "Target Country",
        COUNTRIES,
        index=0,
        help="Restrict lead search to companies based in a specific country"
    )
    if target_country == "Other (type below)":
        target_country = st.text_input("Enter country name", placeholder="e.g. Bangladesh")

with col_d:
    lead_count = st.number_input(
        "How many leads to find?",
        min_value=1, max_value=50, value=5, step=1,
        help="More leads = longer run time and higher API cost. ~$0.03 per lead."
    )

# ── Example Presets ───────────────────────────────────────────────────────────
st.markdown("<p style='color:#64748b;font-size:12px;margin-top:8px;'>Quick presets:</p>", unsafe_allow_html=True)
preset_col1, preset_col2, preset_col3 = st.columns(3)

preset_clicked = None
with preset_col1:
    if st.button("AI Automation → SaaS"):
        preset_clicked = ("Your Name", "YourAgency", "SaaS startups", "AI workflow automation using CrewAI and n8n")
with preset_col2:
    if st.button("SEO → E-commerce"):
        preset_clicked = ("Your Name", "YourAgency", "e-commerce brands on Shopify", "SEO content writing and product page optimization")
with preset_col3:
    if st.button("Dev → Real Estate"):
        preset_clicked = ("Your Name", "YourAgency", "real estate agencies", "custom CRM and lead tracking web apps")

if preset_clicked:
    your_name, company_name, target_industry, your_service = preset_clicked
    st.rerun()

st.markdown("<br>", unsafe_allow_html=True)

# ── Run Button ────────────────────────────────────────────────────────────────
run_clicked = st.button("Launch Agent Crew", use_container_width=True)

# ── Validation & Execution ────────────────────────────────────────────────────
if run_clicked:
    errors = []
    if not openai_key:
        errors.append("OpenAI API key is required")
    if not serper_key:
        errors.append("Serper API key is required")
    if not your_name:
        errors.append("Your name is required")
    if not company_name:
        errors.append("Company name is required")
    if not target_industry:
        errors.append("Target industry is required")
    if not your_service:
        errors.append("Your service is required")
    if target_country == "Other (type below)" and not target_country:
        errors.append("Please enter a country name")

    if errors:
        for e in errors:
            st.error(f"{e}")
    else:
        os.environ["OPENAI_API_KEY"] = openai_key
        os.environ["SERPER_API_KEY"] = serper_key

        st.markdown("<hr>", unsafe_allow_html=True)
        st.markdown("<div class='section-header'><h2>Agent Crew Running</h2></div>", unsafe_allow_html=True)

        # Sound wave animation container (updated per agent phase)
        waveform_container = st.empty()

        def show_waveform(phase="system", label="Processing..."):
            waveform_container.markdown(f"""
            <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:28px 0 12px;">
                <div class="hologram-spinner {phase}"></div>
                <div style="margin-top:16px;font-family:'JetBrains Mono',monospace;font-size:13px;
                            color:var(--text-muted);letter-spacing:0.1em;text-transform:uppercase;
                            animation: pulse-glow 2s infinite alternate;">
                    <span style="color:var(--accent);">></span> {label} <span style="animation: typing-dot 1.5s infinite;">...</span>
                </div>
            </div>
            """, unsafe_allow_html=True)

        show_waveform("system", "Initializing agents...")

        # Live status bar
        st.markdown("""
        <div class="live-status-bar">
            <div class="status-indicator">
                <div class="status-dot active"></div>
                <span class="status-text">SYSTEM ONLINE</span>
            </div>
            <div style="width:1px;height:20px;background:#1e293b;"></div>
            <div class="status-indicator">
                <span class="status-text" style="color:#64748b;">Agents: <span style="color:#06b6d4;">3</span> active</span>
            </div>
            <div class="status-indicator">
                <span class="status-text" style="color:#64748b;">Mode: <span style="color:#10b981;">Sequential</span></span>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Progress
        progress_bar = st.progress(0)
        status_text = st.empty()

        # Terminal log
        st.markdown("""
        <div class="terminal-window">
            <div class="terminal-header">
                <div class="terminal-dot red"></div>
                <div class="terminal-dot yellow"></div>
                <div class="terminal-dot green"></div>
                <span class="terminal-title">agent-crew-log — bash — 80×24</span>
            </div>
            <div class="terminal-body" id="terminal-body">
        """, unsafe_allow_html=True)
        log_container = st.empty()
        log_lines = []

        def update_log(message, progress=None, agent=None):
            now = datetime.now().strftime('%H:%M:%S')
            agent_class = agent if agent else "system"
            agent_label = {"researcher": "AGENT-1", "analyst": "AGENT-2", "writer": "AGENT-3", "system": "SYSTEM"}.get(agent_class, "SYSTEM")
            log_lines.append(f'<div class="terminal-line" style="animation-delay:0s;"><span class="timestamp">{now}</span><span class="agent-tag {agent_class}">{agent_label}</span>{message}</div>')
            log_container.markdown(
                '<div class="terminal-body">' +
                "\n".join(log_lines[-25:]) +
                '<div class="typing-indicator"><span></span><span></span><span></span></div>' +
                '</div>',
                unsafe_allow_html=True
            )
            if progress is not None:
                progress_bar.progress(progress)

        update_log("Initializing Omnix v1.0...", 2, "system")
        time.sleep(0.3)
        update_log(f"Target industry: {target_industry}", 5, "system")
        update_log(f"Country filter: {target_country}", 8, "system")
        update_log(f"Service offering: {your_service}", 10, "system")
        update_log(f"Sender profile: {your_name} @ {company_name}", 12, "system")
        update_log("Loading CrewAI framework...", 15, "system")
        update_log("Mounting SerperDev search tool...", 18, "system")
        update_log("Mounting ScrapeWebsite tool...", 20, "system")

        show_waveform("researcher", "Agent 1 · Lead Researcher · Searching the web...")
        status_text.markdown("""
        <div style="display:flex;align-items:center;gap:10px;padding:10px 0;">
            <div style="width:8px;height:8px;border-radius:50%;background:#06b6d4;box-shadow:0 0 8px rgba(6,182,212,0.5);animation:status-pulse 1.5s infinite;"></div>
            <span style="color:#06b6d4;font-family:JetBrains Mono,monospace;font-size:13px;font-weight:500;">Agent 1 (Lead Researcher) is searching the web...</span>
        </div>
        """, unsafe_allow_html=True)

        try:
            from agents import create_lead_gen_crew

            update_log("Assembling agent crew...", 22, "system")
            update_log("Agent 1: Lead Researcher — ONLINE", 25, "researcher")
            update_log("Agent 2: Company Analyst — STANDBY", 26, "analyst")
            update_log("Agent 3: Email Copywriter — STANDBY", 27, "writer")
            update_log("Crew assembled. Beginning sequential execution...", 30, "system")

            crew, task_leads, task_audit = create_lead_gen_crew(
                company_name=company_name,
                target_industry=target_industry,
                your_service=your_service,
                your_name=your_name,
                lead_count=lead_count,
                target_country=target_country,
                pagespeed_api_key=pagespeed_key,
            )

            update_log("Executing Task 1/3: Research qualified leads...", 35, "researcher")
            time.sleep(0.3)
            update_log(f"Querying Serper API for '{target_industry} {target_country}'...", 40, "researcher")

            result = crew.kickoff()

            # Parse actual leads returned by the crew (may be fewer than requested if some already exist)
            _leads_raw_early = task_leads.output.raw if task_leads.output else ""
            _early_blocks = [b.strip() for b in re.split(r'(?=\n?\d+\.\s)', "\n" + _leads_raw_early.strip()) if b.strip()]
            actual_lead_count = len(_early_blocks)
            already_in_db_count = max(0, lead_count - actual_lead_count)

            update_log(f"Found {actual_lead_count} new leads (of {lead_count} requested)", 50, "researcher")
            update_log("Agent 1: Task complete → Handing off to Website Auditor", 52, "researcher")

            show_waveform("analyst", "Agent 2 · Website Auditor · Running PageSpeed audits...")
            status_text.markdown("""
            <div style="display:flex;align-items:center;gap:10px;padding:10px 0;">
                <div style="width:8px;height:8px;border-radius:50%;background:#8b5cf6;box-shadow:0 0 8px rgba(139,92,246,0.5);animation:status-pulse 1.5s infinite;"></div>
                <span style="color:#8b5cf6;font-family:JetBrains Mono,monospace;font-size:13px;font-weight:500;">Agent 2 (Website Auditor) is running PageSpeed audits...</span>
            </div>
            """, unsafe_allow_html=True)

            update_log("Executing Task 2/3: Audit each company website...", 55, "analyst")
            time.sleep(0.3)
            update_log("Calling PageSpeed Insights API — desktop scores...", 60, "analyst")
            update_log("Calling PageSpeed Insights API — mobile scores...", 65, "analyst")
            update_log("Scanning for chatbots, outdated tech, CTAs...", 70, "analyst")
            update_log("Website audit reports compiled for all leads", 74, "analyst")
            update_log("Agent 2: Task complete → Handing off to Copywriter", 76, "analyst")

            show_waveform("writer", "Agent 3 · Email Copywriter · Writing personalized emails...")
            status_text.markdown("""
            <div style="display:flex;align-items:center;gap:10px;padding:10px 0;">
                <div style="width:8px;height:8px;border-radius:50%;background:#10b981;box-shadow:0 0 8px rgba(16,185,129,0.5);animation:status-pulse 1.5s infinite;"></div>
                <span style="color:#10b981;font-family:JetBrains Mono,monospace;font-size:13px;font-weight:500;">Agent 3 (Email Copywriter) is writing personalized emails...</span>
            </div>
            """, unsafe_allow_html=True)

            update_log("Executing Task 3/3: Compose cold emails...", 80, "writer")
            time.sleep(0.3)
            update_log("Applying personalization hooks from intel briefs...", 85, "writer")
            update_log("Optimizing subject lines for open rates...", 90, "writer")
            update_log("All emails written and formatted", 95, "writer")
            update_log("Pipeline complete. Rendering results...", 98, "system")

            progress_bar.progress(100)
            waveform_container.empty()
            status_text.markdown("""
            <div style="display:flex;align-items:center;gap:10px;padding:10px 0;">
                <div style="width:8px;height:8px;border-radius:50%;background:#10b981;box-shadow:0 0 8px rgba(16,185,129,0.5);"></div>
                <span style="color:#10b981;font-family:JetBrains Mono,monospace;font-size:13px;font-weight:500;">All 3 agents completed successfully!</span>
            </div>
            """, unsafe_allow_html=True)
            update_log("Mission complete! Results rendered below.", 100, "system")

            # ── Results ───────────────────────────────────────────────────────
            st.markdown("<hr>", unsafe_allow_html=True)
            st.markdown("<div class='section-header'><h2>Results</h2></div>", unsafe_allow_html=True)

            # Metrics
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Leads Found", str(actual_lead_count))
            m2.metric("Companies Researched", str(actual_lead_count))
            m3.metric("Emails Written", str(actual_lead_count))
            m4.metric("Agents Deployed", "3")

            if already_in_db_count > 0:
                st.info(
                    f"Got **{actual_lead_count}** new leads — **{already_in_db_count}** "
                    f"were already in your database from previous campaigns."
                )
            else:
                st.success(f"All **{actual_lead_count}** requested leads found successfully!")

            st.markdown("<br>", unsafe_allow_html=True)

            result_text = str(result)
            leads_raw = task_leads.output.raw if task_leads.output else ""

            # Show leads with emails (HTML-escaped to prevent XSS)
            if leads_raw:
                lead_blocks = re.split(r'(?=\n?\d+\.\s)', "\n" + leads_raw.strip())
                lead_blocks = [b.strip() for b in lead_blocks if b.strip()]
                leads_with_email = [
                    b for b in lead_blocks
                    if re.search(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', b)
                ]
                if leads_with_email:
                    st.markdown("<h3 style='font-size:16px;font-weight:700;margin-bottom:12px;'>Leads with Verified Emails</h3>", unsafe_allow_html=True)
                    for lead in leads_with_email:
                        safe_lead = _html_mod.escape(lead)
                        st.markdown(f"""
                        <div class="contact-card">
                            <div style="font-family:JetBrains Mono,monospace;font-size:12px;color:#94a3b8;white-space:pre-wrap;">{safe_lead}</div>
                        </div>
                        """, unsafe_allow_html=True)
                    st.markdown("<br>", unsafe_allow_html=True)

            st.markdown("<h3 style='font-size:16px;font-weight:700;margin-bottom:12px;'>Generated Cold Emails</h3>", unsafe_allow_html=True)
            safe_result = _html_mod.escape(result_text).replace("\n", "<br>")
            st.markdown(f"<div class='result-card'>{safe_result}</div>", unsafe_allow_html=True)

            # Download
            st.markdown("<br>", unsafe_allow_html=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            st.download_button(
                label="Download Full Report (.txt)",
                data=(
                    f"Omnix Report\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                    f"Target: {target_industry}\nCountry: {target_country}\nService: {your_service}\n\n"
                    f"{'='*60}\nLEAD LIST & CONTACT INFO\n{'='*60}\n\n{leads_raw}\n\n"
                    f"{'='*60}\nCOLD EMAILS\n{'='*60}\n\n{result_text}"
                ),
                file_name=f"omnix_report_{timestamp}.txt",
                mime="text/plain",
            )

            # Parse emails, extract website URLs, save to session_state
            from email_sender import parse_emails_from_result
            parsed_emails = parse_emails_from_result(result_text)
            leads_raw_for_parse = task_leads.output.raw if task_leads.output else ""

            # Fill missing To: addresses from the lead list
            found_addresses = re.findall(
                r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b',
                leads_raw_for_parse,
            )
            for i, em in enumerate(parsed_emails):
                if not em.to_address and i < len(found_addresses):
                    em.to_address = found_addresses[i]

            # Extract website URLs from leads output (in lead order)
            website_urls = re.findall(
                r'(?:Website(?:\s*URL)?|Site|Web):\s*(https?://[^\s\n,;)]+)',
                leads_raw_for_parse,
                re.IGNORECASE,
            )
            if len(website_urls) < len(parsed_emails):
                # Fallback: grab all https:// URLs
                all_urls = re.findall(r'https?://[^\s\n,;)]+', leads_raw_for_parse)
                website_urls = all_urls

            # Extract phone numbers from leads output (one per lead, in order)
            phone_numbers = re.findall(
                r'(?:Phone(?:\s+Number)?|Tel(?:ephone)?|Mobile|Cell)[\s:.\-]*'
                r'([\+\d][\d\s\-\.\(\)]{5,20})',
                leads_raw_for_parse,
                re.IGNORECASE,
            )
            phone_numbers = [p.strip() for p in phone_numbers]

            st.session_state["parsed_emails"] = parsed_emails
            st.session_state["result_text"] = result_text
            st.session_state["leads_raw"] = leads_raw_for_parse
            st.session_state["website_urls"] = [u.rstrip(".,;)") for u in website_urls]
            st.session_state["phone_numbers"] = phone_numbers
            st.session_state["your_name"] = your_name
            st.session_state["company_name"] = company_name
            st.session_state["target_industry"] = target_industry

        except Exception as e:
            st.error(f"Error running agents: {str(e)}")
            st.markdown(f"""
            <div style='background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.25);border-radius:12px;padding:20px;font-family:JetBrains Mono,monospace;font-size:12px;color:#f87171;margin-top:12px;'>
            <div style='font-weight:700;margin-bottom:10px;color:#fca5a5;'>Troubleshooting Guide</div>
            • Check your OpenAI API key is valid and has credits<br>
            • Check your Serper API key at serper.dev<br>
            • Ensure you have an active internet connection<br><br>
            <div style='color:#94a3b8;'>Error: {str(e)}</div>
            </div>
            """, unsafe_allow_html=True)

# ── Footer (no run clicked) ───────────────────────────────────────────────────
else:
    if "parsed_emails" not in st.session_state:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("""
        <div class="footer-card">
            <div style="font-family:Inter,sans-serif;font-size:22px;font-weight:800;margin-bottom:8px;color:#f1f5f9;letter-spacing:-0.02em;">
                Ready to find your next clients?
            </div>
            <div style="color:#64748b;font-size:13px;max-width:480px;margin:0 auto 28px;line-height:1.7;">
                Fill in the campaign details above, add your API keys in the sidebar,
                and let three autonomous AI agents research, analyze, and write
                personalized cold emails — fully automated.
            </div>
            <div style="display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-bottom:6px;">
                <span style="background:rgba(6,182,212,0.08);border:1px solid rgba(6,182,212,0.2);color:#22d3ee;padding:5px 14px;border-radius:20px;font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;">CrewAI</span>
                <span style="background:rgba(139,92,246,0.08);border:1px solid rgba(139,92,246,0.2);color:#a78bfa;padding:5px 14px;border-radius:20px;font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;">GPT-4o-mini</span>
                <span style="background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.2);color:#34d399;padding:5px 14px;border-radius:20px;font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;">SerperDev</span>
                <span style="background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.2);color:#fbbf24;padding:5px 14px;border-radius:20px;font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;">Streamlit</span>
                <span style="background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);color:#f87171;padding:5px 14px;border-radius:20px;font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;">SMTP / IMAP</span>
            </div>
        </div>
        """, unsafe_allow_html=True)

# ── Review & Send Section ──────────────────────────────────────────────────────
if "parsed_emails" in st.session_state:
    from email_sender import send_single_email, verify_email
    from database import already_contacted, log_sent, today_count

    _parsed   = st.session_state["parsed_emails"]
    _name     = st.session_state.get("your_name", "")
    _company  = st.session_state.get("company_name", "")
    _industry = st.session_state.get("target_industry", "")
    _urls     = st.session_state.get("website_urls", [])
    _phones   = st.session_state.get("phone_numbers", [])
    _contacts = [em for em in _parsed if em.to_address]

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown("<div class='section-header'><h2>Review Leads Before Sending</h2></div>", unsafe_allow_html=True)
    st.markdown(
        "<p style='color:#64748b;font-size:13px;margin-bottom:20px;'>"
        "Verify each website, review the email, then approve which ones to send. "
        "Already-contacted addresses are automatically flagged.</p>",
        unsafe_allow_html=True,
    )

    if not _contacts:
        st.warning("No contacts with email addresses were found in this campaign.")
    else:
        # Daily progress bar
        _sent_today = today_count()
        _remaining  = max(0, 150 - _sent_today)
        st.markdown(
            f"<div style='background:rgba(6,182,212,0.06);border:1px solid rgba(6,182,212,0.15);"
            f"border-radius:10px;padding:12px 18px;margin-bottom:20px;font-size:12px;"
            f"font-family:JetBrains Mono,monospace;color:#94a3b8;'>"
            f"Daily limit: <b style='color:#06b6d4;'>{_sent_today}</b>/150 sent today"
            f" &nbsp;·&nbsp; <b style='color:#10b981;'>{_remaining}</b> slots remaining</div>",
            unsafe_allow_html=True,
        )

        # Send delay slider
        send_delay = st.slider(
            "Delay between emails (seconds) — prevents spam filters",
            min_value=2, max_value=15, value=5, step=1,
        )

        st.markdown("<br>", unsafe_allow_html=True)

        # Per-lead review cards
        for i, em in enumerate(_contacts):
            is_dup = already_contacted(em.to_address)
            site_url = _urls[i] if i < len(_urls) else ""
            safe_company = _html_mod.escape(em.company)
            safe_to      = _html_mod.escape(em.to_address)
            safe_subject = _html_mod.escape(em.subject[:80])
            safe_url     = _html_mod.escape(site_url)
            safe_phone   = _html_mod.escape(_phones[i]) if i < len(_phones) else ""

            # Badge colors
            dup_badge = (
                "<span style='background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.3);"
                "color:#f87171;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;"
                "font-family:JetBrains Mono,monospace;'>ALREADY CONTACTED</span>"
                if is_dup else ""
            )

            col_cb, col_card = st.columns([0.07, 0.93])
            with col_cb:
                st.checkbox(
                    "", value=(not is_dup), key=f"approve_{i}",
                    disabled=is_dup, help="Uncheck to skip this lead"
                )
            with col_card:
                # Open Website link (so you can verify it's the right company)
                open_link = (
                    f"<a href='{safe_url}' target='_blank' "
                    f"style='color:#06b6d4;font-size:11px;font-family:JetBrains Mono,monospace;"
                    f"text-decoration:none;border:1px solid rgba(6,182,212,0.3);padding:2px 8px;"
                    f"border-radius:6px;'>Open Website ↗</a>"
                    if site_url else ""
                )
                st.markdown(
                    f"<div class='contact-card'>"
                    f"<div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;'>"
                    f"<span style='font-family:Inter,sans-serif;font-weight:700;font-size:14px;color:#e2eaf8;'>{safe_company}</span>"
                    f"{dup_badge}"
                    f"</div>"
                    f"<div style='display:flex;align-items:center;gap:12px;flex-wrap:wrap;'>"
                    f"<span style='font-size:11px;color:#64748b;font-family:JetBrains Mono,monospace;'>{safe_url}</span>"
                    f"{open_link}"
                    f"</div>"
                    f"<div style='font-size:11px;color:#34d399;margin-top:5px;font-family:JetBrains Mono,monospace;'>To: {safe_to}</div>"
                    + (f"<div style='font-size:11px;color:#f59e0b;margin-top:3px;font-family:JetBrains Mono,monospace;'>Tel: {safe_phone}</div>" if safe_phone else "")
                    + f"<div style='font-size:11px;color:#64748b;margin-top:3px;'>Subject: {safe_subject}{'...' if len(em.subject) > 80 else ''}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                with st.expander("Preview Email"):
                    safe_body = _html_mod.escape(em.body)
                    st.markdown(
                        f"<div style='font-family:JetBrains Mono,monospace;font-size:12px;"
                        f"color:#94a3b8;white-space:pre-wrap;line-height:1.7;'>{safe_body}</div>",
                        unsafe_allow_html=True,
                    )

        # Collect approved leads
        approved = [
            em for i, em in enumerate(_contacts)
            if st.session_state.get(f"approve_{i}", True)
            and not already_contacted(em.to_address)
        ]
        skipped_dup = sum(1 for em in _contacts if already_contacted(em.to_address))

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown(
            f"<p style='font-size:13px;color:#94a3b8;font-family:JetBrains Mono,monospace;'>"
            f"<b style='color:#10b981;'>{len(approved)}</b> approved to send"
            + (f" &nbsp;·&nbsp; <b style='color:#64748b;'>{skipped_dup}</b> already contacted" if skipped_dup else "")
            + f" &nbsp;·&nbsp; daily slots remaining: <b style='color:#06b6d4;'>{_remaining}</b></p>",
            unsafe_allow_html=True,
        )

        if not gmail_sidebar:
            st.warning("Add your sender email in the sidebar before sending.")
        else:
            st.markdown(
                f"<div style='background:rgba(16,185,129,0.06);border:1px solid rgba(16,185,129,0.15);"
                f"border-radius:10px;padding:10px 16px;font-size:12px;color:#34d399;margin-bottom:12px;"
                f"font-family:JetBrains Mono,monospace;'>Sending from: "
                f"<b style='color:#e2eaf8;'>{_html_mod.escape(gmail_sidebar)}</b></div>",
                unsafe_allow_html=True,
            )

        send_clicked = st.button(
            f"Send {len(approved)} Approved Email(s)",
            use_container_width=True,
            key="send_approved_btn",
            disabled=(len(approved) == 0),
        )

        if send_clicked:
            if not gmail_sidebar or not gpass_sidebar:
                st.error("Add your email and password in the sidebar first.")
            elif not smtp_host_sidebar:
                st.error("SMTP host is empty — select cPanel / Hosting and enter your mail server hostname (e.g. srv31.easyhost.pk).")
            elif _remaining <= 0:
                st.error("Daily limit of 150 reached. Come back tomorrow.")
            else:
                to_send = approved[:_remaining]  # Never exceed daily limit
                st.markdown(
                    f"<p style='color:#8b5cf6;font-size:13px;font-weight:600;'>"
                    f"Sending {len(to_send)} email(s) — {send_delay}s delay between each...</p>",
                    unsafe_allow_html=True,
                )
                send_prog = st.progress(0)
                send_results = []

                for idx, em in enumerate(to_send):
                    # Verify address exists before sending — skip guessed addresses (info@, contact@, etc.)
                    if not verify_email(em.to_address):
                        r = {
                            "success": False,
                            "company": em.company,
                            "error": (
                                f"Skipped — {em.to_address} failed verification "
                                "(address likely does not exist)"
                            ),
                        }
                        send_results.append(r)
                        send_prog.progress((idx + 1) / len(to_send))
                        continue

                    r = send_single_email(
                        smtp_host=smtp_host_sidebar,
                        smtp_port=smtp_port_sidebar,
                        sender_email=gmail_sidebar,
                        sender_password=gpass_sidebar,
                        to_address=em.to_address,
                        subject=em.subject,
                        body=em.body,
                        from_name=_name,
                        sender_company=_company,
                        imap_host=imap_host_sidebar,
                        imap_port=imap_port_sidebar,
                    )
                    r["company"] = em.company
                    if r["success"]:
                        log_sent(em.company, em.to_address, em.subject, campaign=_industry)
                    send_results.append(r)
                    send_prog.progress((idx + 1) / len(to_send))
                    if send_delay > 0 and idx < len(to_send) - 1:
                        time.sleep(send_delay)

                st.markdown("<br>", unsafe_allow_html=True)
                ok  = sum(1 for r in send_results if r["success"])
                bad = len(send_results) - ok
                if ok:
                    st.success(f"{ok} email(s) sent successfully!")
                if bad:
                    st.warning(f"{bad} email(s) failed.")

                for r in send_results:
                    safe_co = _html_mod.escape(r["company"])
                    if r["success"]:
                        st.markdown(
                            f"<div style='background:rgba(16,185,129,0.06);border:1px solid rgba(16,185,129,0.15);"
                            f"border-radius:10px;padding:10px 16px;margin:5px 0;font-size:13px;display:flex;"
                            f"align-items:center;gap:10px;'>"
                            f"<div style='width:6px;height:6px;border-radius:50%;background:#10b981;'></div>"
                            f"<b style='color:#34d399;'>{safe_co}</b> — Sent</div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        safe_err = _html_mod.escape(r.get("error", "Unknown error"))
                        st.markdown(
                            f"<div style='background:rgba(239,68,68,0.06);border:1px solid rgba(239,68,68,0.15);"
                            f"border-radius:10px;padding:10px 16px;margin:5px 0;font-size:13px;display:flex;"
                            f"align-items:center;gap:10px;'>"
                            f"<div style='width:6px;height:6px;border-radius:50%;background:#ef4444;'></div>"
                            f"<b style='color:#f87171;'>{safe_co}</b> — {safe_err}</div>",
                            unsafe_allow_html=True,
                        )
