"""Legal job sources (official/public APIs only) + helpers for text and language detection."""
import base64
import html
import re
import time
from datetime import datetime, timedelta, timezone

import requests

TIMEOUT = 30
UA = {"User-Agent": "JobFinder/1.0 (private use)"}

WORKTIME_LABELS = {"vz": "Vollzeit", "tz": "Teilzeit", "mj": "Minijob", "ws": "Werkstudent"}
_BA_WORKTIME = {"vz": "vz", "tz": "tz", "mj": "mj", "ws": "tz"}  # BA files Werkstudent under Teilzeit
LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]


# ---------------------------------------------------------------- helpers
def html_to_text(raw):
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def keyword_regex(keywords):
    """Short all-caps words like IT/SQL match case-sensitive as whole words, the rest case-insensitive."""
    parts = []
    for k in keywords:
        k = k.strip()
        if not k:
            continue
        esc = re.escape(k)
        # short words (IT, SQL) as whole words; longer ones also inside compounds (Wirtschaftsinformatik)
        parts.append(rf"(?-i:\b{esc}\b)" if len(k) <= 3 else esc)
    return re.compile("|".join(parts), re.I) if parts else None


def exclude_regex(words):
    """Words the user never wants to see. Whole words by default, so excluding "Senior"
    does not silently hide "Seniorenbetreuung". A * widens one end: "Lead*" also catches
    Leader and Leadership, "*lead" catches Teamlead, "*lead*" catches both."""
    parts = []
    for word in words:
        word = word.strip()
        core = re.escape(word.strip("*"))
        if not core:
            continue
        left = "" if word.startswith("*") else r"\b"
        right = "" if word.endswith("*") else r"\b"
        parts.append(f"{left}{core}{right}")
    return re.compile("|".join(parts), re.I) if parts else None


def location_parts(location):
    """Split "Remote oder Köln / Darmstadt / Berlin" into separate place names."""
    return [p.strip() for p in re.split(r"[/,;|]|\boder\b|\bund\b", location or "", flags=re.I) if p.strip()]


_WORD_LEVELS = [
    (r"muttersprach|verhandlungssicher|native", "C2"),
    (r"flie(ß|ss)end|sehr gute|fluent|excellent", "C1"),
    (r"\bgute\b|good", "B2"),
    (r"grundkenntnis|basic", "A2"),
]


# a clause ends at a list separator or at the end of a sentence ("… von Vorteil. Polnisch A2")
_CLAUSE_SEP = re.compile(r"[\n•;,|]|(?<=[a-zäöüßA-ZÄÖÜ])\.(?=\s|$)")

# code -> (label, how the language is named in a German or English ad)
LANGUAGES = {
    "de": ("Немецкий", r"deutsch(?!land|e bahn|en bahn)|german(?!y)"),
    "en": ("Английский", r"englisch|english"),
    "fr": ("Французский", r"franz(?:ö|oe)sisch|french|français"),
    "es": ("Испанский", r"spanisch|spanish|espa(?:ñ|n)ol"),
    "it": ("Итальянский", r"italienisch|italian\b|italiano"),
    "nl": ("Нидерландский", r"niederl(?:ä|ae)ndisch|dutch|nederlands"),
    "pl": ("Польский", r"polnisch|polish"),
    "ru": ("Русский", r"russisch|russian"),
    "uk": ("Украинский", r"ukrainisch|ukrainian"),
    "tr": ("Турецкий", r"t(?:ü|ue)rkisch|turkish"),
    "pt": ("Португальский", r"portugiesisch|portuguese|portugu(?:ê|e)s"),
    "ar": ("Арабский", r"arabisch|arabic"),
    "zh": ("Китайский", r"chinesisch|chinese|mandarin"),
}


# every language name plus a CEFR level, for highlighting inside a description
LANGUAGE_HIGHLIGHT = re.compile(
    "|".join(rf"(?:{pattern})\w*" for _, pattern in LANGUAGES.values()) + r"|\b[ABC][12]\b", re.I)


def _clause_around(text, start, end):
    """The clause the match sits in plus its offset, so "Deutsch B1, Englisch C2" is not read as C2."""
    left = max([m.end() for m in _CLAUSE_SEP.finditer(text, 0, start)] + [start - 70, 0])
    after = _CLAUSE_SEP.search(text, end)
    return text[left:min(after.start() if after else len(text), end + 70, len(text))], left


def detect_levels(text):
    """Rough required level per language: {"de": "B2", "en": "?"}.
    "?" = the language is named but no level is given; a missing key = not mentioned at all."""
    text = text or ""
    result = {}
    for code, (_, pattern) in LANGUAGES.items():
        found = []
        mentioned = False
        for m in re.finditer(pattern, text, re.I):
            mentioned = True
            window, left = _clause_around(text, m.start(), m.end())
            here = m.start() - left
            candidates = [(abs(lm.start() - here), lm.group(1))
                          for lm in re.finditer(r"\b([ABC][12])\b", window)]
            for word_pattern, level in _WORD_LEVELS:
                candidates += [(abs(wm.start() - here), level)
                               for wm in re.finditer(word_pattern, window, re.I)]
            if not candidates:
                continue
            # one clause can name several languages, so trust the level word nearest to this one;
            # overlapping wordings ("sehr gute" inside "gute") sit close together, take the strongest
            nearest = min(d for d, _ in candidates)
            found.append(max((lvl for d, lvl in candidates if d <= nearest + 12), key=LEVELS.index))
        if found:
            result[code] = max(found, key=LEVELS.index)
        elif mentioned:
            result[code] = "?"
    return result


# ---------------------------------------------------------------- hours per week and employment kind
EMPLOYMENT_LABELS = {"vz": "Vollzeit", "tz": "Teilzeit", "ws": "Werkstudent",
                     "mj": "Minijob", "pr": "Praktikum"}

_EMPLOYMENT_WORDS = {
    "ws": r"werkstudent|working\s?student|studentische[rn]?\s+(?:aushilfe|mitarbeit)",
    "pr": r"praktik|internship|\bintern\b|trainee",
    "mj": r"minijob|mini-job|geringf(?:ü|ue)gig",
    "vz": r"vollzeit|full[\s-]?time",
    "tz": r"teilzeit|part[\s-]?time",
}

_WEEK_WORD = re.compile(r"woche|week|w(?:ö|oe)chentlich|weekly", re.I)
_HOURS_RE = re.compile(
    r"(\d{1,2}(?:[.,]\d)?)"                                  # 20, 38,5
    r"(?:\s*(?:-|–|—|bis)\s*(\d{1,2}(?:[.,]\d)?))?"          # optional "20-30"
    r"\s*-?\s*(?:h\b|std\.?|stunden|hours?)", re.I)


def detect_hours(text):
    """Weekly hours the ad states, as (low, high). None when it says nothing."""
    text = text or ""
    spans = []
    for m in _HOURS_RE.finditer(text):
        # "20 Stunden" alone means nothing; it has to be tied to a week
        if not _WEEK_WORD.search(text[max(0, m.start() - 35):m.end() + 35]):
            continue
        low = float(m.group(1).replace(",", "."))
        high = float(m.group(2).replace(",", ".")) if m.group(2) else low
        if low > high:
            low, high = high, low
        if 1 <= low and high <= 60:  # 60+ per week is a typo or an annual figure
            spans.append((low, high))
    if not spans:
        return None
    return min(s[0] for s in spans), max(s[1] for s in spans)


def format_hours(low, high):
    def trim(v):
        return f"{v:g}".replace(".", ",")
    if low is None:
        return ""
    return f"{trim(low)} ч" if low == high else f"{trim(low)}–{trim(high)} ч"


def detect_employment(text, ba_flags=None):
    """Which employment kinds the ad offers, as codes: ["tz", "ws"]."""
    codes = []
    if ba_flags:  # Arbeitsagentur states these as proper fields, so trust them first
        if ba_flags.get("arbeitszeitVollzeit"):
            codes.append("vz")
        if any(v for k, v in ba_flags.items() if k.startswith("arbeitszeitTeilzeit")):
            codes.append("tz")
        if ba_flags.get("istGeringfuegigeBeschaeftigung"):
            codes.append("mj")
    for code, pattern in _EMPLOYMENT_WORDS.items():
        if code not in codes and re.search(pattern, text or "", re.I):
            codes.append(code)
    return [c for c in EMPLOYMENT_LABELS if c in codes]


def format_employment(codes, low=None, high=None):
    """Table cell: "Werkstudent, 20 ч"."""
    names = ", ".join(EMPLOYMENT_LABELS[c] for c in (codes or []) if c in EMPLOYMENT_LABELS)
    hours = format_hours(low, high)
    return ", ".join(p for p in (names, hours) if p)


def matches_employment(codes, wanted):
    """Empty `wanted` keeps everything. Once a kind is asked for, an ad whose kind
    could not be read is dropped too — otherwise "only Werkstudent" would not mean that."""
    return not wanted or bool(set(codes or ()) & set(wanted))


def matches_languages(required, mine, keep_unclear=True):
    """Can someone with `mine` ({"de": "B1"}) apply to an ad needing `required`?"""
    for code, need in (required or {}).items():
        have = mine.get(code)
        if need not in LEVELS:  # named without a level
            if have is None and not keep_unclear:
                return False
            continue
        if have is None or LEVELS.index(have) < LEVELS.index(need):
            return False
    return True


def format_languages(required):
    """Short cell for the table: "DE B2 · EN C1"."""
    return " · ".join(f"{code.upper()} {level}" for code, level in (required or {}).items())


def detect_german(text):
    """Kept for the old single-language callers. None = no level found."""
    level = detect_levels(text).get("de")
    return level if level in LEVELS else None


def mentions_english(text):
    return bool(re.search(r"englisch|english", text or "", re.I))


def enrich(job, ba_flags=None):
    """Fill language, hours and employment fields from whatever the ad already carries."""
    # only the title and the board's own worktime field name the kind of contract;
    # the description says things like "abgeschlossene Ausbildung", which is a requirement
    job["employment"] = detect_employment(f"{job.get('title', '')} {job.get('worktime', '')}", ba_flags)
    job["hours_min"], job["hours_max"] = detect_hours(job.get("description")) or (None, None)
    if job.get("description") is not None:
        levels = detect_levels(job["description"])
        job["languages"] = levels
        job["german"] = levels.get("de") or "?"
        job["english"] = int("en" in levels)
    return job


# ---------------------------------------------------------------- Arbeitsagentur
BA_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
BA_HEADERS = {"X-API-Key": "jobboerse-jobsuche", **UA}


def _ba_list(params):
    last_error = None
    for path in ("/pc/v4/jobs", "/pc/v6/jobs"):  # v4 first, v6 as fallback
        try:
            r = requests.get(BA_BASE + path, headers=BA_HEADERS, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            last_error = str(e)
            continue
        if r.status_code == 200:
            return r.json()
        last_error = f"HTTP {r.status_code}"
    raise RuntimeError(last_error)


def _ba_place(it):
    """Location: new schema keeps it in stellenlokationen[].adresse, older one in arbeitsort."""
    for loc in it.get("stellenlokationen") or []:
        ort = ((loc.get("adresse") or {}).get("ort") or "").strip()
        if ort:
            return ort
    place = it.get("arbeitsort") or {}
    return (place.get("ort") or "").strip() if isinstance(place, dict) else str(place)


def _ba_title(it):
    """The API pads the title with the ad's locations, joined by a comma without a space.
    Commas the employer typed come with a space, so only the space-less ones are cut."""
    title = (it.get("titel") or it.get("stellenangebotsTitel") or it.get("beruf") or "").strip()
    parts = re.split(r",(?!\s)", title)
    if len(parts) == 1:
        return title
    towns = {((loc.get("adresse") or {}).get("ort") or "").strip().casefold()
             for loc in it.get("stellenlokationen") or []}
    while len(parts) > 1 and _looks_like_place(parts[-1].strip(), towns):
        parts.pop()
    return ",".join(parts).strip()


def _looks_like_place(part, towns):
    if part.casefold() in towns:
        return True
    if part.casefold().startswith(("mobiles arbeiten", "homeoffice", "home-office")):
        return True
    # a bare town name: a few capitalised words, no digits and no (m/w/d)-style markers
    return bool(re.fullmatch(r"[A-ZÄÖÜ][^\d()/]{0,40}", part)) and len(part.split()) <= 4


def _ba_published(it):
    for value in (it.get("aktuelleVeroeffentlichungsdatum"), it.get("datumErsteVeroeffentlichung"),
                  (it.get("veroeffentlichungszeitraum") or {}).get("von"), it.get("aenderungsdatum")):
        if value:
            return str(value)[:10]
    return ""


def ba_search(keyword, cfg, remote):
    params = {
        "was": keyword,
        "angebotsart": cfg["angebotsart"],
        "veroeffentlichtseit": cfg["days"],
        "size": 100,
        "zeitarbeit": str(cfg["zeitarbeit"]).lower(),
        "pav": "false",
    }
    if remote:
        params["arbeitszeit"] = "ho"
    else:
        params.update(wo=cfg["city"], umkreis=cfg["radius"])
        if cfg["worktime"]:
            params["arbeitszeit"] = ";".join(dict.fromkeys(_BA_WORKTIME[w] for w in cfg["worktime"]))

    jobs = []
    for page in range(1, cfg["max_pages"] + 1):
        params["page"] = page
        data = _ba_list(params)
        items = data.get("stellenangebote") or data.get("ergebnisliste")
        if items is None:
            items = next((v for v in data.values() if isinstance(v, list)), [])
        for it in items:
            ref = it.get("refnr") or it.get("referenznummer")
            if not ref:
                continue
            job = enrich({
                "id": f"ba:{ref}",
                "source": "Arbeitsagentur",
                "ref": ref,
                "title": _ba_title(it),
                "company": it.get("arbeitgeber") or it.get("firma") or "",
                "location": _ba_place(it),
                "remote": int(remote or it.get("homeofficemoeglich") or False),
                "worktime": "Homeoffice" if remote else ", ".join(WORKTIME_LABELS[w] for w in cfg["worktime"]),
                "url": (it.get("externeUrl") or it.get("externeURL")
                        or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}"),
                "published": _ba_published(it),
                "description": None,  # loaded separately
                "keyword": keyword,
            }, ba_flags=it)
            # the query echo said nothing about this ad; the API's own flags do
            job["worktime"] = "Homeoffice" if remote else ", ".join(
                EMPLOYMENT_LABELS[c] for c in job["employment"] if c in EMPLOYMENT_LABELS)
            jobs.append(job)
        total = int(data.get("maxErgebnisse") or 0)
        if len(items) < 100 or page * 100 >= total:
            break
        time.sleep(0.4)
    return jobs


def ba_description(ref):
    """Returns text, '' if the ad is gone, None if it failed (retry later)."""
    code = base64.b64encode(ref.encode()).decode()
    gone = False
    for path in (f"/pc/v4/jobdetails/{code}", f"/pc/v3/jobdetails/{code}"):
        try:
            r = requests.get(BA_BASE + path, headers=BA_HEADERS, timeout=TIMEOUT)
        except requests.RequestException:
            continue
        if r.status_code == 200:
            d = r.json()
            return html_to_text(d.get("stellenangebotsBeschreibung") or d.get("stellenbeschreibung") or "")
        if r.status_code == 404:
            gone = True
    return "" if gone else None


# ---------------------------------------------------------------- Arbeitnow
ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
_WT_WORDS = {"vz": ("full", "vollzeit"), "tz": ("part", "teilzeit", "werkstudent"), "mj": ("mini",)}


def _worktime_matches(types, selected):
    known = [w for words in _WT_WORDS.values() for w in words]
    if not any(w in types for w in known):
        return True  # no info about hours -> keep
    return any(w in types for s in selected for w in _WT_WORDS.get(s, ()))


def arbeitnow_search(cfg, kw_re):
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["days"])
    jobs = []
    for page in range(1, cfg["arbeitnow_pages"] + 1):
        r = requests.get(ARBEITNOW_URL, params={"page": page}, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        items = r.json().get("data", [])
        if not items:
            break
        all_old = True
        for it in items:
            created = datetime.fromtimestamp(it.get("created_at") or 0, tz=timezone.utc)
            if created < cutoff:
                continue
            all_old = False
            desc = html_to_text(it.get("description", ""))
            hay = f"{it.get('title', '')} {' '.join(it.get('tags') or [])} {desc}"
            match = kw_re.search(hay) if kw_re else None
            if kw_re and not match:
                continue
            remote = bool(it.get("remote"))
            loc = it.get("location") or ""
            in_city = cfg["city"].lower() in loc.lower()
            if not ((cfg["mode"] in ("office", "both") and in_city) or (cfg["mode"] in ("remote", "both") and remote)):
                continue
            types = " ".join(it.get("job_types") or []).lower()
            if cfg["worktime"] and not _worktime_matches(types, cfg["worktime"]):
                continue
            jobs.append(enrich({
                "id": f"an:{it.get('slug')}",
                "source": "Arbeitnow",
                "ref": it.get("slug"),
                "title": it.get("title", ""),
                "company": it.get("company_name", ""),
                "location": loc,
                "remote": int(remote),
                "worktime": ", ".join(it.get("job_types") or []),
                "url": it.get("url", ""),
                "published": created.date().isoformat(),
                "description": desc,
                "keyword": match.group(0) if match else "",
            }))
        if all_old:
            break
        time.sleep(0.5)
    return jobs


# ---------------------------------------------------------------- Adzuna (needs free key)
def adzuna_search(cfg, keywords, app_id, app_key):
    """Berlin/office search only. Descriptions from Adzuna are truncated."""
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what_or": " ".join(keywords),
        "where": cfg["city"],
        "distance": cfg["radius"],
        "max_days_old": cfg["days"],
        "results_per_page": 50,
        "content-type": "application/json",
    }
    if cfg["worktime"] == ["vz"]:
        params["full_time"] = 1
    elif cfg["worktime"] == ["tz"]:
        params["part_time"] = 1

    jobs = []
    for page in range(1, cfg["adzuna_pages"] + 1):
        r = requests.get(f"https://api.adzuna.com/v1/api/jobs/de/search/{page}", params=params, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        items = r.json().get("results", [])
        for it in items:
            ct = it.get("contract_time") or ""
            jobs.append(enrich({
                "id": f"az:{it.get('id')}",
                "source": "Adzuna",
                "ref": str(it.get("id")),
                "title": html_to_text(it.get("title", "")),
                "company": (it.get("company") or {}).get("display_name", ""),
                "location": (it.get("location") or {}).get("display_name", ""),
                "remote": 0,
                "worktime": {"full_time": "Vollzeit", "part_time": "Teilzeit"}.get(ct, ""),
                "url": it.get("redirect_url", ""),
                "published": (it.get("created") or "")[:10],
                "description": html_to_text(it.get("description", "")) + "\n\n(Adzuna: описание сокращено, полный текст по ссылке)",
                "keyword": "",
            }))
        if len(items) < 50:
            break
        time.sleep(0.5)
    return jobs


# ---------------------------------------------------------------- remote boards (no key needed)
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs"
REMOTEOK_URL = "https://remoteok.com/api"

# these boards state where a candidate may sit; keep only what someone in Germany can take
_OPEN_TO_DE = re.compile(r"germany|deutschland|europe|emea|worldwide|anywhere|global|international", re.I)


def _open_to_germany(geo):
    """An empty location on these boards means the ad is open worldwide."""
    geo = (geo or "").strip()
    return not geo or bool(_OPEN_TO_DE.search(geo))


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _remote_job(cfg, kw_re, *, source, prefix, ref, title, company, geo, url, published, desc, tags, worktime):
    """Shared filtering for the remote boards. None = the ad is filtered out."""
    if published and published < datetime.now(timezone.utc) - timedelta(days=cfg["days"]):
        return None
    if cfg["remote_eu_only"] and not _open_to_germany(geo):
        return None
    match = kw_re.search(f"{title} {tags} {desc}") if kw_re else None
    if kw_re and not match:
        return None
    return enrich({
        "id": f"{prefix}:{ref}",
        "source": source,
        "ref": str(ref),
        "title": title,
        "company": company,
        "location": (geo or "").strip() or "Worldwide",
        "remote": 1,
        "worktime": worktime,
        "url": url,
        "published": published.date().isoformat() if published else "",
        "description": desc,
        "keyword": match.group(0) if match else "",
    })


def _remote_queries(cfg):
    """One unfiltered pass plus one per keyword: a single response is capped well below
    what these boards hold. Short words are skipped — the boards reject them as tags."""
    keywords = [k.strip() for k in cfg["keywords"] if len(k.strip()) >= 3]
    return [None] + keywords[:6]


def _fetch_merged(url, base_params, cfg, param_name):
    """Merge the responses of several keyword queries. Raises only if every one failed,
    so one rejected tag never hides a board that is otherwise answering."""
    payloads, errors = [], []
    for query in _remote_queries(cfg):
        params = dict(base_params)
        if query:
            params[param_name] = query
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            if r.status_code == 400:  # the board did not accept this tag
                continue
            r.raise_for_status()
        except requests.RequestException as e:
            errors.append(str(e))
            continue
        payloads.append(r.json())
        time.sleep(0.3)
    if not payloads and errors:
        raise RuntimeError(errors[0])
    return payloads


def remotive_search(cfg, kw_re):
    r = requests.get(REMOTIVE_URL, params={"limit": cfg["remote_limit"]}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for it in r.json().get("jobs", []):
        job = _remote_job(
            cfg, kw_re, source="Remotive", prefix="rv", ref=it.get("id"),
            title=it.get("title", ""), company=(it.get("company_name") or "").strip(),
            geo=it.get("candidate_required_location"), url=it.get("url", ""),
            published=_parse_dt(it.get("publication_date")),
            desc=html_to_text(it.get("description", "")),
            tags=" ".join(it.get("tags") or []),
            worktime=(it.get("job_type") or "").replace("_", " "))
        if job:
            jobs.append(job)
    return jobs


def jobicy_search(cfg, kw_re):
    params = {"count": 50}  # 50 is the API's own ceiling per response
    if cfg["remote_eu_only"]:
        params["geo"] = "germany"  # Jobicy reads this as "open to candidates sitting in Germany"
    seen, jobs = set(), []
    for payload in _fetch_merged(JOBICY_URL, params, cfg, "tag"):
        for it in payload.get("jobs", []):
            if it.get("id") in seen:
                continue
            seen.add(it.get("id"))
            job = _remote_job(
                cfg, kw_re, source="Jobicy", prefix="jy", ref=it.get("id"),
                title=it.get("jobTitle", ""), company=(it.get("companyName") or "").strip(),
                geo=it.get("jobGeo"), url=it.get("url", ""),
                published=_parse_dt(it.get("pubDate")),
                desc=html_to_text(it.get("jobDescription") or it.get("jobExcerpt") or ""),
                tags=" ".join(it.get("jobIndustry") or []),
                worktime=", ".join(it.get("jobType") or []))
            if job:
                jobs.append(job)
    return jobs


def remoteok_search(cfg, kw_re):
    seen, jobs = set(), []
    for payload in _fetch_merged(REMOTEOK_URL, {}, cfg, "tags"):
        for it in payload:
            ref = it.get("id") or it.get("slug")
            if not it.get("position") or ref in seen:  # the first element is a legal notice
                continue
            seen.add(ref)
            job = _remote_job(
                cfg, kw_re, source="RemoteOK", prefix="rok", ref=ref,
                title=it.get("position", ""), company=(it.get("company") or "").strip(),
                geo=it.get("location"), url=it.get("url") or it.get("apply_url", ""),
                published=_parse_dt(it.get("date")),
                desc=html_to_text(it.get("description", "")),
                tags=" ".join(it.get("tags") or []),
                worktime="")
            if job:
                jobs.append(job)
    return jobs
