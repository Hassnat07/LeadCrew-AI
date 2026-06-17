"""
SQLite-backed email log.
Tracks every sent email to enforce deduplication and the 150/day limit.
"""
import sqlite3
import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads_sent.db")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS sent_emails (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                company   TEXT    NOT NULL,
                to_email  TEXT    NOT NULL,
                domain    TEXT,
                subject   TEXT,
                campaign  TEXT,
                sent_at   TEXT    NOT NULL,
                sent_date TEXT    NOT NULL
            )
        """)
        # Unique index prevents double-sending the same address
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_email ON sent_emails(to_email)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_date ON sent_emails(sent_date)"
        )

        # v2: audit-first lead pipeline
        c.execute("""
            CREATE TABLE IF NOT EXISTS seen_businesses (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL,
                domain        TEXT,
                phone         TEXT,
                niche         TEXT,
                city          TEXT,
                rating        REAL,
                rating_count  INTEGER,
                pain_score    INTEGER,
                issues_json   TEXT,
                status        TEXT    DEFAULT 'harvested',
                audited_at    TEXT,
                website       TEXT,
                address        TEXT,
                category       TEXT,
                no_website     INTEGER DEFAULT 0,
                website_source TEXT
            )
        """)
        # Partial unique indexes: NULL / empty values are allowed to repeat;
        # only non-empty domain/phone values must be unique.
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_biz_domain "
            "ON seen_businesses(domain) WHERE domain IS NOT NULL AND domain != ''"
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_biz_phone "
            "ON seen_businesses(phone) WHERE phone IS NOT NULL AND phone != ''"
        )
        # Migrations: add columns to existing databases
        for _col in (
            "ALTER TABLE seen_businesses ADD COLUMN website_source TEXT",
            "ALTER TABLE seen_businesses ADD COLUMN email TEXT",
            "ALTER TABLE seen_businesses ADD COLUMN email_source TEXT",
            "ALTER TABLE seen_businesses ADD COLUMN phone_source TEXT",
            "ALTER TABLE seen_businesses ADD COLUMN social_links TEXT",
            "ALTER TABLE seen_businesses ADD COLUMN grade TEXT",
            "ALTER TABLE seen_businesses ADD COLUMN market TEXT",
            "ALTER TABLE seen_businesses ADD COLUMN harvested_at TEXT",
        ):
            try:
                c.execute(_col)
            except Exception:
                pass  # column already exists

        # Search history — tracks what niche/city combos have been harvested
        c.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                niche       TEXT NOT NULL,
                city        TEXT NOT NULL,
                country     TEXT NOT NULL DEFAULT '',
                market      TEXT NOT NULL DEFAULT 'email',
                ran_at      TEXT NOT NULL,
                leads_found INTEGER DEFAULT 0
            )
        """)

        # v2: email drafts (created by write_emails.py)
        c.execute("""
            CREATE TABLE IF NOT EXISTS email_drafts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id    INTEGER NOT NULL,
                to_email       TEXT,
                subject        TEXT NOT NULL,
                body           TEXT NOT NULL,
                language       TEXT NOT NULL DEFAULT 'en',
                model          TEXT,
                generated_at   TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'draft'
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_draft_biz ON email_drafts(business_id)"
        )

        # v2: follow-up tracker (3-day follow-ups for non-repliers)
        c.execute("""
            CREATE TABLE IF NOT EXISTS followups (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id       INTEGER NOT NULL,
                sent_draft_id     INTEGER NOT NULL,
                followup_draft_id INTEGER,
                sent_at           TEXT NOT NULL,
                due_at            TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'pending'
            )
        """)

        # v2: send-gap state (one row, enforces 3-10 min gap between dashboard sends)
        c.execute("""
            CREATE TABLE IF NOT EXISTS send_gap (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                last_sent_at TEXT,
                next_ok_at   TEXT
            )
        """)
        try:
            c.execute("INSERT INTO send_gap (id) VALUES (1)")
        except Exception:
            pass  # row already exists

        # v2: PageSpeed 30-day cache
        c.execute("""
            CREATE TABLE IF NOT EXISTS pagespeed_cache (
                domain            TEXT PRIMARY KEY,
                mobile_score      INTEGER,
                desktop_score     INTEGER,
                fcp_s             REAL,
                lcp_s             REAL,
                total_byte_weight INTEGER,
                viewport_ok       INTEGER,
                cached_at         TEXT    NOT NULL
            )
        """)


def already_contacted(email: str) -> bool:
    """Return True if this email address was already sent to."""
    _init()
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM sent_emails WHERE to_email = ? LIMIT 1",
            (email.lower().strip(),),
        ).fetchone()
        return row is not None


def log_sent(company: str, to_email: str, subject: str = "", campaign: str = ""):
    """Record a successfully sent email. Silently ignores duplicates."""
    _init()
    email = to_email.lower().strip()
    domain = email.split("@")[-1] if "@" in email else ""
    now = datetime.now()
    with _conn() as c:
        try:
            c.execute(
                """INSERT INTO sent_emails
                   (company, to_email, domain, subject, campaign, sent_at, sent_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (company, email, domain, subject, campaign,
                 now.isoformat(), now.date().isoformat()),
            )
        except sqlite3.IntegrityError:
            pass  # Already logged — unique constraint on to_email


def today_count() -> int:
    """Number of emails sent today."""
    _init()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM sent_emails WHERE sent_date = ?",
            (date.today().isoformat(),),
        ).fetchone()
        return row["n"] if row else 0


def total_count() -> int:
    """Total emails ever sent."""
    _init()
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM sent_emails").fetchone()
        return row["n"] if row else 0


def recent_sent(limit: int = 30) -> list[dict]:
    """Most recent sent emails, newest first."""
    _init()
    with _conn() as c:
        rows = c.execute(
            """SELECT company, to_email, sent_at, campaign
               FROM sent_emails ORDER BY sent_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── v2: seen_businesses table ─────────────────────────────────────────────────

def add_business(biz: dict) -> bool:
    """
    Insert a new business record.
    Returns True if inserted, False if a domain/phone conflict blocked it.
    """
    _init()
    with _conn() as c:
        try:
            c.execute(
                """INSERT INTO seen_businesses
                       (name, domain, phone, niche, city, rating, rating_count,
                        pain_score, issues_json, status, audited_at,
                        website, address, category, no_website, website_source,
                        social_links, harvested_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    biz.get("name"),       biz.get("domain"),      biz.get("phone"),
                    biz.get("niche"),      biz.get("city"),
                    biz.get("rating"),     biz.get("rating_count"),
                    biz.get("pain_score"), biz.get("issues_json"),
                    biz.get("status", "harvested"), biz.get("audited_at"),
                    biz.get("website"),    biz.get("address"),
                    biz.get("category"),   biz.get("no_website", 0),
                    biz.get("website_source"), biz.get("social_links"),
                    datetime.now().isoformat(),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def domain_seen(domain: str) -> bool:
    """Return True if this domain already exists in seen_businesses."""
    _init()
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM seen_businesses WHERE domain = ? LIMIT 1",
            (domain.lower().strip(),),
        ).fetchone()
        return row is not None


def name_city_seen(name: str, city: str) -> bool:
    """
    Return True if a business with this name and city is already stored.

    Accepts both the bare city ('Manchester') and the country-suffixed form
    ('Manchester, UK') so old un-normalized rows are matched correctly until
    clean_db.py has normalized the column.
    """
    _init()
    city_norm = city.split(",")[0].strip()
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM seen_businesses WHERE name = ? "
            "AND (city = ? OR city LIKE ?) LIMIT 1",
            (name.strip(), city_norm, city_norm + ",%"),
        ).fetchone()
        return row is not None


def get_businesses(status: str = None, limit: int = 2000) -> list[dict]:
    """Return businesses, optionally filtered by status, sorted by pain_score desc."""
    _init()
    with _conn() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM seen_businesses WHERE status = ? "
                "ORDER BY pain_score DESC NULLS LAST LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM seen_businesses ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def update_business_status(domain: str, status: str, **kwargs) -> None:
    """Update status and any extra fields by domain."""
    _init()
    fields = {"status": status, **kwargs}
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as c:
        c.execute(
            f"UPDATE seen_businesses SET {set_clause} WHERE domain = ?",
            [*fields.values(), domain],
        )


def update_business_by_id(biz_id: int, **kwargs) -> None:
    """Update arbitrary fields on a business row by its primary key."""
    _init()
    if not kwargs:
        return
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    with _conn() as c:
        c.execute(
            f"UPDATE seen_businesses SET {set_clause} WHERE id = ?",
            [*kwargs.values(), biz_id],
        )


def reset_city(city: str) -> int:
    """Delete all seen_businesses rows for a given city. Returns count deleted."""
    _init()
    with _conn() as c:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM seen_businesses WHERE city = ?",
            (city,),
        ).fetchone()["n"]
        c.execute("DELETE FROM seen_businesses WHERE city = ?", (city,))
        return n


def businesses_count(status: str = None) -> int:
    """Count businesses, optionally filtered by status."""
    _init()
    with _conn() as c:
        if status:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM seen_businesses WHERE status = ?",
                (status,),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM seen_businesses",
            ).fetchone()
        return row["n"] if row else 0


# ── v2: PageSpeed 30-day cache ────────────────────────────────────────────────

def get_pagespeed_cache(domain: str) -> dict | None:
    """Return cached PageSpeed data for domain if < 30 days old; None otherwise."""
    _init()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM pagespeed_cache WHERE domain = ? LIMIT 1",
            (domain.lower().strip(),),
        ).fetchone()
    if not row:
        return None
    try:
        if datetime.fromisoformat(row["cached_at"]) < datetime.now() - timedelta(days=30):
            return None
    except Exception:
        return None
    return dict(row)


def save_pagespeed_cache(domain: str, data: dict) -> None:
    """Insert or replace PageSpeed data for this domain."""
    _init()
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO pagespeed_cache
               (domain, mobile_score, desktop_score, fcp_s, lcp_s,
                total_byte_weight, viewport_ok, cached_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                domain.lower().strip(),
                data.get("mobile_score"),
                data.get("desktop_score"),
                data.get("fcp_s"),
                data.get("lcp_s"),
                data.get("total_byte_weight"),
                data.get("viewport_ok"),
                datetime.now().isoformat(),
            ),
        )


# ── email_drafts helpers ──────────────────────────────────────────────────────

def get_draft(draft_id: int) -> dict | None:
    """Return one draft joined with its business row, or None."""
    _init()
    with _conn() as c:
        row = c.execute(
            """SELECT d.*, b.name AS biz_name, b.city, b.pain_score, b.grade,
                      b.rating, b.rating_count, b.issues_json, b.no_website, b.niche
               FROM email_drafts d
               JOIN seen_businesses b ON b.id = d.business_id
               WHERE d.id = ?""",
            (draft_id,),
        ).fetchone()
        return dict(row) if row else None


def update_draft_status(draft_id: int, status: str) -> None:
    _init()
    with _conn() as c:
        c.execute(
            "UPDATE email_drafts SET status = ? WHERE id = ?",
            (status, draft_id),
        )


def get_draft_dates() -> list[dict]:
    """Return distinct dates that have pending drafts, newest first, with counts."""
    _init()
    with _conn() as c:
        rows = c.execute(
            """SELECT DATE(generated_at) AS draft_date, COUNT(*) AS cnt
               FROM email_drafts
               WHERE status = 'draft'
               GROUP BY draft_date
               ORDER BY draft_date DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def delete_drafts_by_date(date_str: str) -> int:
    """Delete all pending drafts generated on date_str (YYYY-MM-DD). Returns count deleted."""
    _init()
    with _conn() as c:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM email_drafts "
            "WHERE status = 'draft' AND DATE(generated_at) = ?",
            (date_str,),
        ).fetchone()["n"]
        c.execute(
            "DELETE FROM email_drafts "
            "WHERE status = 'draft' AND DATE(generated_at) = ?",
            (date_str,),
        )
        return n


def get_leads_with_drafts(status: str = "draft") -> list[dict]:
    """
    All qualified leads (website issues + no-website with email) that have a
    draft in the given status. Newest batch first, then grade/pain_score.
    """
    _init()
    with _conn() as c:
        rows = c.execute(
            """SELECT b.id AS biz_id, b.name, b.city, b.grade, b.pain_score,
                      b.rating, b.rating_count, b.email, b.issues_json, b.no_website,
                      d.id AS draft_id, d.subject, d.body, d.status AS draft_status,
                      d.generated_at, d.language
               FROM seen_businesses b
               JOIN email_drafts d ON d.business_id = b.id
               WHERE b.status = 'qualified'
                 AND d.status = ?
               ORDER BY
                 d.generated_at DESC,
                 CASE b.grade WHEN 'A+' THEN 0 WHEN 'A' THEN 1 ELSE 2 END,
                 b.pain_score DESC,
                 b.rating_count DESC""",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_no_website_leads_full() -> list[dict]:
    """No-website leads without an email (email leads are in the email draft queue)."""
    _init()
    with _conn() as c:
        rows = c.execute(
            """SELECT id, name, phone, rating, rating_count, city, address,
                      niche, category, grade, pain_score, social_links, market
               FROM seen_businesses
               WHERE status = 'qualified'
                 AND (no_website = 1 OR market = 'whatsapp')
                 AND (email IS NULL OR email = '')
               ORDER BY id DESC,
                        CASE grade WHEN 'A+' THEN 0 WHEN 'A' THEN 1 ELSE 2 END,
                        pain_score DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


# ── Stale-lead cleanup ────────────────────────────────────────────────────────

def cleanup_stale_leads(retention_days: int = 3) -> dict:
    """
    Delete leads and drafts that are older than retention_days and were never
    contacted.  Returns {"drafts": N, "leads": N}.

    NEVER deletes:
      - Anything whose email is in sent_emails (already emailed)
      - Businesses referenced in followups (followup = email was sent)
      - Businesses with a 'sent'/'discarded'/'skipped'/'bad_email' draft
        (user took explicit action)
      - seen_businesses rows with NULL harvested_at (pre-migration rows)
    """
    _init()
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()

    with _conn() as c:
        # 1. Stale email_drafts: status='draft', generated_at < cutoff,
        #    business email not already in sent_emails.
        stale_ids = [
            r[0] for r in c.execute(
                """SELECT d.id FROM email_drafts d
                   LEFT JOIN seen_businesses b ON b.id = d.business_id
                   WHERE d.status = 'draft'
                     AND d.generated_at < ?
                     AND (
                           b.email IS NULL OR b.email = ''
                           OR NOT EXISTS (
                               SELECT 1 FROM sent_emails s WHERE s.to_email = b.email
                           )
                         )""",
                (cutoff,),
            ).fetchall()
        ]
        if stale_ids:
            c.execute(
                f"DELETE FROM email_drafts WHERE id IN ({','.join('?' * len(stale_ids))})",
                stale_ids,
            )
        drafts_deleted = len(stale_ids)

        # 2. Stale seen_businesses: harvested_at set and < cutoff,
        #    never sent (not in sent_emails, not in followups),
        #    no actioned draft remaining (all stale drafts already gone above).
        biz_deleted = c.execute(
            """DELETE FROM seen_businesses
               WHERE harvested_at IS NOT NULL
                 AND harvested_at < ?
                 AND status IN ('harvested', 'audited', 'qualified', 'disqualified')
                 AND id NOT IN (SELECT business_id FROM followups)
                 AND (
                       email IS NULL OR email = ''
                       OR NOT EXISTS (
                           SELECT 1 FROM sent_emails s WHERE s.to_email = email
                       )
                     )
                 AND id NOT IN (
                       SELECT business_id FROM email_drafts
                       WHERE status IN ('sent', 'discarded', 'skipped', 'bad_email')
                     )""",
            (cutoff,),
        ).rowcount

    return {"drafts": drafts_deleted, "leads": biz_deleted}


# ── followups helpers ─────────────────────────────────────────────────────────

def log_followup_needed(business_id: int, sent_draft_id: int, sent_at: str) -> None:
    """Record that a follow-up is due 3 days after sent_at."""
    _init()
    due = (datetime.fromisoformat(sent_at) + timedelta(days=3)).isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO followups
               (business_id, sent_draft_id, sent_at, due_at, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (business_id, sent_draft_id, sent_at, due),
        )


def get_due_followups() -> list[dict]:
    """Return pending follow-ups whose due_at has passed, joined with business info."""
    _init()
    now = datetime.now().isoformat()
    with _conn() as c:
        rows = c.execute(
            """SELECT f.id AS followup_id, f.business_id, f.sent_draft_id,
                      f.followup_draft_id, f.sent_at, f.due_at, f.status,
                      b.name, b.city, b.email, b.pain_score, b.grade,
                      d.subject AS original_subject, d.language AS draft_language
               FROM followups f
               JOIN seen_businesses b ON b.id = f.business_id
               JOIN email_drafts d ON d.id = f.sent_draft_id
               WHERE f.status = 'pending' AND f.due_at <= ?
               ORDER BY f.due_at""",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_followup(followup_id: int, **kwargs) -> None:
    _init()
    if not kwargs:
        return
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    with _conn() as c:
        c.execute(
            f"UPDATE followups SET {set_clause} WHERE id = ?",
            [*kwargs.values(), followup_id],
        )


def get_all_followups() -> list[dict]:
    """All followup rows with business + draft info for the dashboard."""
    _init()
    with _conn() as c:
        rows = c.execute(
            """SELECT f.id AS followup_id, f.business_id, f.sent_draft_id,
                      f.followup_draft_id, f.sent_at, f.due_at, f.status,
                      b.name, b.city, b.email, b.grade, b.pain_score,
                      d.subject AS original_subject
               FROM followups f
               JOIN seen_businesses b ON b.id = f.business_id
               JOIN email_drafts d ON d.id = f.sent_draft_id
               ORDER BY f.due_at DESC""",
        ).fetchall()
        return [dict(r) for r in rows]


# ── send_gap helpers ──────────────────────────────────────────────────────────

def get_send_gap() -> dict:
    _init()
    with _conn() as c:
        row = c.execute("SELECT last_sent_at, next_ok_at FROM send_gap WHERE id = 1").fetchone()
        return dict(row) if row else {}


def set_send_gap(last_sent_at: str, next_ok_at: str) -> None:
    _init()
    with _conn() as c:
        c.execute(
            "UPDATE send_gap SET last_sent_at = ?, next_ok_at = ? WHERE id = 1",
            (last_sent_at, next_ok_at),
        )


# ── search history helpers ────────────────────────────────────────────────────

def log_search(niche: str, city: str, country: str, market: str, leads_found: int) -> None:
    """Record a completed harvest run in search_history."""
    _init()
    with _conn() as c:
        c.execute(
            "INSERT INTO search_history (niche, city, country, market, ran_at, leads_found) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (niche, city, country, market, datetime.now().isoformat(), leads_found),
        )


def get_search_history(limit: int = 10) -> list[dict]:
    """Recent harvest runs, newest first."""
    _init()
    with _conn() as c:
        rows = c.execute(
            "SELECT niche, city, country, market, ran_at, leads_found "
            "FROM search_history ORDER BY ran_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
