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


def set_status(job_id, status):
    with _conn() as c:
        c.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))


def set_note(job_id, note):
    with _conn() as c:
        c.execute("UPDATE jobs SET note=? WHERE id=?", (note, job_id))


def load_jobs():
    with _conn() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY published DESC, first_seen DESC").fetchall()
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


def load_settings():
    if SETTINGS_PATH.exists():
        try:
            return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))}
        except json.JSONDecodeError:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
