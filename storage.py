"""Local storage: SQLite for jobs/statuses, JSON for search settings."""
import json
import sqlite3
from datetime import date
from pathlib import Path

import sources

BASE = Path(__file__).parent
DB_PATH = BASE / "jobs.db"
SETTINGS_PATH = BASE / "settings.json"

DEFAULT_SETTINGS = {
    "keywords": ["Excel", "IT", "Informatik", "Python", "SQL", "Datenanalyse"],
    "exclude": [],
    "worktime": ["vz", "tz"],
    "mode": "both",
    "city": "Berlin",
    "radius": 25,
    "days": 7,
    "angebotsart": 1,
    "zeitarbeit": False,
    "use_ba": True,
    "use_an": True,
    "use_ad": False,
    "use_rv": True,
    "use_jy": True,
    "use_rok": True,
    "my_languages": {},  # empty = no language filtering until the user names their own
    "keep_unclear_languages": True,
    "remote_eu_only": True,
    "remote_limit": 200,
    "adzuna_app_id": "",
    "adzuna_app_key": "",
    "max_pages": 2,
    "arbeitnow_pages": 5,
    "adzuna_pages": 2,
}

_COLUMNS = ["id", "source", "ref", "title", "company", "location", "remote", "worktime",
            "url", "published", "description", "german", "english", "languages",
            "employment", "hours_min", "hours_max", "keyword", "first_seen"]


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, source TEXT, ref TEXT, title TEXT, company TEXT,
                location TEXT, remote INTEGER, worktime TEXT, url TEXT, published TEXT,
                description TEXT, german TEXT, english INTEGER, keyword TEXT,
                first_seen TEXT, status TEXT DEFAULT 'neu', note TEXT DEFAULT ''
            )""")
        columns = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        if "languages" not in columns:  # added when multi-language filtering arrived
            c.execute("ALTER TABLE jobs ADD COLUMN languages TEXT")
        for name, kind in (("employment", "TEXT"), ("hours_min", "REAL"), ("hours_max", "REAL")):
            if name not in columns:  # added when weekly-hours filtering arrived
                c.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                name TEXT PRIMARY KEY, created TEXT, last_seen TEXT, settings TEXT
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_jobs (
                user TEXT, job_id TEXT, status TEXT DEFAULT 'neu', note TEXT DEFAULT '',
                PRIMARY KEY (user, job_id)
            )""")
        # ads stored before that column existed still have their text, so read the levels off it
        stale = c.execute("SELECT id, title, description FROM jobs "
                          "WHERE languages IS NULL OR employment IS NULL").fetchall()
        for row in stale:
            desc = row["description"]
            low, high = sources.detect_hours(desc) or (None, None)
            c.execute("UPDATE jobs SET languages=?, employment=?, hours_min=?, hours_max=? WHERE id=?",
                      (json.dumps(sources.detect_levels(desc), ensure_ascii=False),
                       ",".join(sources.detect_employment(row["title"] or "")),
                       low, high, row["id"]))


def upsert(jobs):
    today = date.today().isoformat()
    with _conn() as c:
        for j in jobs:
            row = {col: j.get(col) for col in _COLUMNS}
            row["languages"] = json.dumps(j.get("languages") or {}, ensure_ascii=False)
            row["employment"] = ",".join(j.get("employment") or [])
            row["first_seen"] = today
            c.execute(f"""
                INSERT INTO jobs ({', '.join(_COLUMNS)})
                VALUES ({', '.join(':' + col for col in _COLUMNS)})
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    published = excluded.published,
                    description = COALESCE(jobs.description, excluded.description),
                    german = COALESCE(jobs.german, excluded.german),
                    english = COALESCE(jobs.english, excluded.english),
                    languages = COALESCE(jobs.languages, excluded.languages),
                    employment = COALESCE(jobs.employment, excluded.employment),
                    hours_min = COALESCE(jobs.hours_min, excluded.hours_min),
                    hours_max = COALESCE(jobs.hours_max, excluded.hours_max)
            """, row)


def missing_descriptions(ids):
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    with _conn() as c:
        rows = c.execute(
            f"SELECT id, ref FROM jobs WHERE source='Arbeitsagentur' AND description IS NULL AND id IN ({marks})",
            list(ids)).fetchall()
    return [(r["id"], r["ref"]) for r in rows]


def set_description(job_id, text):
    levels = sources.detect_levels(text)
    low, high = sources.detect_hours(text) or (None, None)
    with _conn() as conn:
        row = conn.execute("SELECT title, employment FROM jobs WHERE id=?", (job_id,)).fetchone()
        known = [c for c in ((row["employment"] or "").split(",") if row else []) if c]
        found = sources.detect_employment(row["title"] if row else "")
        codes = [c for c in sources.EMPLOYMENT_LABELS if c in set(known) | set(found)]
        conn.execute(
            "UPDATE jobs SET description=?, german=?, english=?, languages=?, "
            "employment=?, hours_min=?, hours_max=? WHERE id=?",
            (text, levels.get("de") or "?", int("en" in levels),
             json.dumps(levels, ensure_ascii=False), ",".join(codes), low, high, job_id))


# ---------------------------------------------------------------- accounts
# A name, no password: this separates people's lists, it does not protect them.
# Anyone who types someone else's name gets that person's list.
def list_users():
    with _conn() as c:
        return [r["name"] for r in c.execute("SELECT name FROM users ORDER BY lower(name)")]


def find_user(name):
    """Case-insensitive lookup, so "Таня" and "таня" are the same person. Compared in
    Python, not SQL: SQLite's own lower() only folds ASCII and leaves Cyrillic alone.
    Returns the spelling stored at sign-up, or None."""
    name = (name or "").strip().casefold()
    if not name:
        return None
    with _conn() as c:
        rows = c.execute("SELECT name FROM users").fetchall()
    return next((r["name"] for r in rows if r["name"].casefold() == name), None)


def create_user(name):
    name = (name or "").strip()
    existing = find_user(name)
    if existing:  # a second "таня" must land in Таня's list, not start an empty one
        return existing
    today = date.today().isoformat()
    with _conn() as c:
        first = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
        c.execute("INSERT INTO users (name, created, last_seen, settings) VALUES (?, ?, ?, ?)",
                  (name, today, today, json.dumps(DEFAULT_SETTINGS, ensure_ascii=False)))
        if first:
            # stars and notes made before accounts existed belong to whoever sets up first
            c.execute("""
                INSERT OR IGNORE INTO user_jobs (user, job_id, status, note)
                SELECT ?, id, COALESCE(status, 'neu'), COALESCE(note, '') FROM jobs
                WHERE (status IS NOT NULL AND status != 'neu') OR (note IS NOT NULL AND note != '')
            """, (name,))
            if SETTINGS_PATH.exists():
                try:
                    c.execute("UPDATE users SET settings=? WHERE name=?",
                              (SETTINGS_PATH.read_text(encoding="utf-8"), name))
                except OSError:
                    pass
    return name


def touch_user(name):
    with _conn() as c:
        c.execute("UPDATE users SET last_seen=? WHERE name=?", (date.today().isoformat(), name))


def set_status(user, job_id, status):
    with _conn() as c:
        c.execute("""INSERT INTO user_jobs (user, job_id, status) VALUES (?, ?, ?)
                     ON CONFLICT(user, job_id) DO UPDATE SET status=excluded.status""",
                  (user, job_id, status))


def set_note(user, job_id, note):
    with _conn() as c:
        c.execute("""INSERT INTO user_jobs (user, job_id, note) VALUES (?, ?, ?)
                     ON CONFLICT(user, job_id) DO UPDATE SET note=excluded.note""",
                  (user, job_id, note))


def load_jobs(user):
    """The ads are shared; the status and the note come from this user's own rows."""
    columns = ", ".join(f"jobs.{col}" for col in _COLUMNS)
    with _conn() as c:
        rows = c.execute(f"""
            SELECT {columns},
                   COALESCE(uj.status, 'neu') AS status,
                   COALESCE(uj.note, '') AS note
            FROM jobs LEFT JOIN user_jobs uj ON uj.job_id = jobs.id AND uj.user = ?
            ORDER BY jobs.published DESC, jobs.first_seen DESC""", (user,)).fetchall()
    jobs = []
    for r in rows:
        job = dict(r)
        try:
            job["languages"] = json.loads(job["languages"]) if job["languages"] else {}
        except (TypeError, ValueError):
            job["languages"] = {}
        job["employment"] = [c for c in (job["employment"] or "").split(",") if c]
        jobs.append(job)
    return jobs


def load_settings(user=None):
    if user:
        with _conn() as c:
            row = c.execute("SELECT settings FROM users WHERE name=?", (user,)).fetchone()
        if row and row["settings"]:
            try:
                return {**DEFAULT_SETTINGS, **json.loads(row["settings"])}
            except json.JSONDecodeError:
                pass
    return dict(DEFAULT_SETTINGS)


def save_settings(user, settings):
    with _conn() as c:
        c.execute("UPDATE users SET settings=? WHERE name=?",
                  (json.dumps(settings, ensure_ascii=False), user))
