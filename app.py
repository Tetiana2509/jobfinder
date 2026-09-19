"""Job Finder UI. Start with:  streamlit run app.py"""
import html
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import pandas as pd
import streamlit as st

import sources
import storage

st.set_page_config(page_title="Job Finder", page_icon="🔎", layout="wide")
storage.init()

STATUS = {"neu": "Новая", "gemerkt": "⭐ Избранное", "beworben": "✅ Отклик отправлен", "ausgeblendet": "Скрыта"}
MODES = {"office": "Офис", "remote": "Удалённо (вся Германия)", "both": "Офис + удалённо"}
ANGEBOT = {1: "Работа", 34: "Praktikum / Trainee", 4: "Ausbildung / Duales Studium"}

# ================================================================ who is this
def enter_as(name):
    st.session_state["user"] = name
    st.query_params["user"] = name  # so a refresh does not throw you back to the login screen
    st.rerun()


def login_screen():
    st.title("🔎 Job Finder")
    st.caption("Список вакансий общий, а избранное, отклики и заметки — у каждого свои. "
               "Пароля нет: имя только разделяет списки, но не защищает их.")
    typed = st.text_input("Имя", placeholder="например, Таня").strip()
    if typed:
        existing = storage.find_user(typed)
        if existing:
            st.success(f"Есть такой: **{existing}**.")
            if st.button(f"Войти как {existing}", type="primary", key="enter_typed", width="stretch"):
                enter_as(existing)
        else:
            st.info(f"Пользователя «{typed}» ещё нет.")
            if st.button(f"Создать «{typed}»", type="primary", key="make_new", width="stretch"):
                enter_as(storage.create_user(typed))
    st.stop()


user = st.session_state.get("user") or storage.find_user(st.query_params.get("user", ""))
if not user:
    login_screen()
st.session_state["user"] = user
storage.touch_user(user)
S = storage.load_settings(user)

st.markdown("""
<style>
.job-desc {max-height: 62vh; overflow-y: auto; padding: 1rem 1.2rem; border: 1px solid rgba(128,128,128,.3);
           border-radius: 6px; line-height: 1.55; font-size: .95rem; white-space: normal;}
.job-desc mark {background: #ffe58a; color: inherit; padding: 0 2px; border-radius: 2px;}
.job-desc mark.lang {background: #bde3ff;}
</style>""", unsafe_allow_html=True)

# ================================================================ sidebar: search settings
with st.sidebar:
    who, out = st.columns([2, 1], vertical_alignment="bottom")
    who.caption(f"Ты вошла как **{user}**")
    if out.button("Выйти", width="stretch"):
        st.session_state.pop("user", None)
        st.query_params.clear()
        st.rerun()

    st.header("Поиск")
    kw_text = st.text_area("Ключевые слова (по одному в строке)", "\n".join(S["keywords"]), height=170)
    # a multiselect, not a text area: st.text_area only commits on blur or Ctrl+Enter,
    # so a typed word would sit there without ever filtering anything
    excl_words = st.multiselect("Исключить слова", S["exclude"], default=S["exclude"],
                                accept_new_options=True, placeholder="Senior, Lead, Zeitarbeit",
                                help="Впиши слово и нажми Enter. Вакансия скрывается, если это слово "
                                     "есть в заголовке или названии компании. Сравнение по целым словам, "
                                     "чтобы «Senior» не задел «Seniorenbetreuung». Звёздочка расширяет: "
                                     "«Lead*» поймает и Leader, «*lead» — Teamlead, «*lead*» — оба.")
    worktime = st.multiselect("Arbeitszeit", list(sources.WORKTIME_LABELS), default=S["worktime"],
                              format_func=sources.WORKTIME_LABELS.get)
    mode = st.radio("Где", list(MODES), index=list(MODES).index(S["mode"]), format_func=MODES.get)
    city = st.text_input("Город для офиса", S["city"])
    radius = st.slider("Радиус, км", 0, 100, S["radius"], step=5)
    days = st.slider("Опубликовано за последние N дней", 1, 60, S["days"])
    angebot = st.selectbox("Тип", list(ANGEBOT), index=list(ANGEBOT).index(S["angebotsart"]), format_func=ANGEBOT.get)
    zeitarbeit = st.checkbox("Показывать Zeitarbeit", S["zeitarbeit"])

    st.subheader("Мои языки")
    saved_langs = S["my_languages"]
    picked = st.multiselect("Какими владею", list(sources.LANGUAGES),
                            default=[c for c in saved_langs if c in sources.LANGUAGES],
                            format_func=lambda c: sources.LANGUAGES[c][0])
    my_languages = {c: st.select_slider(sources.LANGUAGES[c][0], sources.LEVELS,
                                        value=saved_langs.get(c, "B1"), key=f"lvl_{c}")
                    for c in picked}
    keep_unclear = st.checkbox("Считать подходящими вакансии, где уровень не указан",
                               S["keep_unclear_languages"])

    st.subheader("Источники")
    use_ba = st.checkbox("Arbeitsagentur", S["use_ba"])
    use_an = st.checkbox("Arbeitnow", S["use_an"])
    with st.expander("Adzuna (нужен бесплатный ключ)"):
        ad_id = st.text_input("App ID", os.getenv("ADZUNA_APP_ID") or S["adzuna_app_id"])
        ad_key = st.text_input("App Key", os.getenv("ADZUNA_APP_KEY") or S["adzuna_app_key"], type="password")
        st.caption("Ключ: developer.adzuna.com. Ищет только по городу.")
    use_ad = st.checkbox("Adzuna", S["use_ad"] and bool(ad_id and ad_key), disabled=not (ad_id and ad_key))

    st.caption("Удалённая работа — ключи не нужны")
    use_rv = st.checkbox("Remotive", S["use_rv"])
    use_jy = st.checkbox("Jobicy", S["use_jy"])
    use_rok = st.checkbox("RemoteOK", S["use_rok"])
    remote_eu_only = st.checkbox("Только те, где можно работать из Германии/ЕС", S["remote_eu_only"])
    st.caption("Ищут по всему миру, поэтому город и радиус к ним не применяются. "
               "Фиды у них небольшие: если вакансий мало, увеличь окно по дням.")

    run = st.button("Искать", type="primary", width="stretch")

exclude_words = [w.strip() for w in excl_words if w.strip()]
exclude_re = sources.exclude_regex(exclude_words)

# ================================================================ search
if run:
    keywords = [k.strip() for k in kw_text.splitlines() if k.strip()]
    cfg = {**S, "keywords": keywords, "exclude": exclude_words, "worktime": worktime, "mode": mode, "city": city.strip() or "Berlin",
           "radius": radius, "days": days, "angebotsart": angebot, "zeitarbeit": zeitarbeit,
           "use_ba": use_ba, "use_an": use_an, "use_ad": use_ad, "adzuna_app_id": ad_id, "adzuna_app_key": ad_key,
           "use_rv": use_rv, "use_jy": use_jy, "use_rok": use_rok, "remote_eu_only": remote_eu_only,
           "my_languages": my_languages, "keep_unclear_languages": keep_unclear}
    storage.save_settings(user, cfg)

    found, errors = {}, []
    with st.status("Ищу вакансии…", expanded=True) as status:
        if use_ba:
            variants = [v for v, on in ((False, mode in ("office", "both")), (True, mode in ("remote", "both"))) if on]
            for kw in keywords:
                for remote in variants:
                    st.write(f"Arbeitsagentur: «{kw}» {'удалённо' if remote else cfg['city']}")
                    try:
                        for j in sources.ba_search(kw, cfg, remote):
                            found.setdefault(j["id"], j)
                    except Exception as e:
                        errors.append(f"Arbeitsagentur «{kw}»: {e}")
        if use_an:
            st.write("Arbeitnow")
            try:
                for j in sources.arbeitnow_search(cfg, sources.keyword_regex(keywords)):
                    found.setdefault(j["id"], j)
            except Exception as e:
                errors.append(f"Arbeitnow: {e}")
        if mode in ("remote", "both"):
            kw_re = sources.keyword_regex(keywords)
            remote_boards = [("Remotive", use_rv, sources.remotive_search),
                             ("Jobicy", use_jy, sources.jobicy_search),
                             ("RemoteOK", use_rok, sources.remoteok_search)]
            for name, enabled, search in remote_boards:
                if not enabled:
                    continue
                try:
                    hits = search(cfg, kw_re)
                    for j in hits:
                        found.setdefault(j["id"], j)
                    st.write(f"{name}: {len(hits)}")
                except Exception as e:
                    errors.append(f"{name}: {e}")
        if use_ad and mode in ("office", "both"):
            st.write("Adzuna")
            try:
                for j in sources.adzuna_search(cfg, keywords, ad_id, ad_key):
                    found.setdefault(j["id"], j)
            except Exception as e:
                errors.append(f"Adzuna: {e}")

        storage.upsert(list(found.values()))

        missing = storage.missing_descriptions(list(found))
        if missing:
            bar = st.progress(0.0, text="Загружаю описания…")
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(sources.ba_description, ref): job_id for job_id, ref in missing}
                for i, fut in enumerate(as_completed(futures), 1):
                    text = fut.result()
                    if text is not None:
                        storage.set_description(futures[fut], text)
                    bar.progress(i / len(missing), text=f"Описания: {i} из {len(missing)}")

        status.update(label=f"Готово: найдено {len(found)}", state="complete", expanded=False)

    st.session_state["last_ids"] = set(found)
    for e in errors:
        st.warning(e)

# ================================================================ list filters
jobs = storage.load_jobs(user)
if not jobs:
    st.info("Пока пусто. Настрой фильтры слева и нажми «Искать».")
    st.stop()

f1, f2, f3, f4 = st.columns([2, 2, 1.9, 1.7])
status_view = f1.selectbox("Показать", ["active", *STATUS],
                           format_func=lambda s: "Все, кроме скрытых" if s == "active" else STATUS[s])
text_filter = f2.text_input("Фильтр по тексту", placeholder="например: SAP, Werkstudent")
employment_filter = f3.multiselect("Занятость", list(sources.EMPLOYMENT_LABELS),
                                   format_func=sources.EMPLOYMENT_LABELS.get,
                                   help="Пусто — показывать любую. Если выбрать, вакансии "
                                        "с неопределённым типом скрываются.")
max_hours = f4.slider("Часов в неделю не больше", 0, 45, 0,
                      help="0 — не ограничивать. Часы берутся из текста вакансии; "
                           "если они там не указаны, вакансия остаётся в списке.")

city_counts = Counter(p for j in jobs for p in sources.location_parts(j["location"]))
g1, g2, g3, g4 = st.columns([2.4, 2, 2, 2])
city_choice = g1.multiselect("Город", [c for c, _ in city_counts.most_common()],
                             help="Только для офисных вакансий — при режиме «Удалённо» фильтр не нужен. "
                                  "Пусто — показывать все города.") if mode != "remote" else []
only_my_languages = g2.checkbox("Подходят под мои языки", bool(my_languages),
                                disabled=not my_languages,
                                help="Выбери свои языки слева, чтобы включить этот фильтр. "
                                     "Он скроет вакансии, где нужен язык или уровень выше твоего.")
show_unknown = g3.checkbox("Без данных о языке тоже", True)
only_last = g4.checkbox("Только последний поиск", bool(st.session_state.get("last_ids")))

today = date.today().isoformat()
last_ids = st.session_state.get("last_ids", set())
text_re = re.compile(re.escape(text_filter.strip()), re.I) if text_filter.strip() else None


def visible(j):
    if status_view == "active" and j["status"] == "ausgeblendet":
        return False
    if status_view != "active" and j["status"] != status_view:
        return False
    if only_last and last_ids and j["id"] not in last_ids:
        return False
    if exclude_re and exclude_re.search(f"{j['title']} {j['company']}"):
        return False
    if city_choice and not any(c.casefold() in (j["location"] or "").casefold() for c in city_choice):
        return False
    if not sources.matches_employment(j["employment"], employment_filter):
        return False
    if max_hours and j["hours_min"] and j["hours_min"] > max_hours:
        return False
    langs = j["languages"] or {}
    if not langs and not show_unknown:
        return False
    if only_my_languages and not sources.matches_languages(langs, my_languages, keep_unclear):
        return False
    if text_re and not text_re.search(f"{j['title']} {j['company']} {j['description'] or ''}"):
        return False
    return True


shown = [j for j in jobs if visible(j)]
st.caption(f"Показано {len(shown)} из {len(jobs)}")
if not shown:
    st.info("Под эти фильтры ничего не подходит. Ослабь фильтр по уровню или тексту.")
    st.stop()

# ================================================================ list + detail
left, right = st.columns([1.15, 1], gap="large")

with left:
    df = pd.DataFrame([{
        "": ("🆕" if j["first_seen"] == today and j["status"] == "neu" else "")
            + {"gemerkt": "⭐", "beworben": "✅", "ausgeblendet": "🙈"}.get(j["status"], ""),
        "Должность": j["title"],
        "Компания": j["company"],
        "Место": ("🏠 " if j["remote"] else "") + (j["location"] or ""),
        "Занятость": sources.format_employment(j["employment"], j["hours_min"], j["hours_max"]),
        "Языки": sources.format_languages(j["languages"]) or "…",
        "Дата": j["published"],
        "Источник": j["source"],
    } for j in shown])
    event = st.dataframe(df, on_select="rerun", selection_mode="single-row", hide_index=True,
                         height=640, width="stretch",
                         column_config={"": st.column_config.TextColumn(width=45),
                                        "Языки": st.column_config.TextColumn(width=110),
                                        "Занятость": st.column_config.TextColumn(width=165)})
    rows = event.selection.rows
    if rows:
        st.session_state["selected"] = shown[rows[0]]["id"]

    export = df.drop(columns=[""]).assign(Link=[j["url"] for j in shown])
    st.download_button("Скачать CSV для Excel", export.to_csv(sep=";", index=False).encode("utf-8-sig"),
                       file_name=f"jobs_{today}.csv", mime="text/csv")

with right:
    job = next((j for j in shown if j["id"] == st.session_state.get("selected")), None)
    if job is None:
        st.info("Выбери вакансию в таблице слева, чтобы увидеть описание.")
        st.stop()

    st.subheader(job["title"])
    meta = [job["company"], ("Удалённо, " if job["remote"] else "") + (job["location"] or ""),
            sources.format_employment(job["employment"], job["hours_min"], job["hours_max"]) or job["worktime"],
            job["published"], job["source"]]
    st.caption("  |  ".join(m for m in meta if m))

    langs = job["languages"] or {}
    if langs:
        needed = ", ".join(
            f"**{sources.LANGUAGES[c][0]}** " + (lv if lv in sources.LEVELS else "(уровень не указан)")
            for c, lv in langs.items() if c in sources.LANGUAGES)
        fits = sources.matches_languages(langs, my_languages, keep_unclear)
        st.markdown(f"Языки: {needed}" + ("" if not my_languages else "  |  " + ("✅ подходит" if fits else "⚠️ выше моего уровня")))
    else:
        st.markdown("Языки: *в описании не упомянуты*")

    b1, b2, b3, b4 = st.columns(4)
    actions = [(b1, "gemerkt", "⭐ В избранное"), (b2, "beworben", "✅ Откликнулась"),
               (b3, "ausgeblendet", "Скрыть"), (b4, "neu", "Сбросить")]
    for col, new_status, label in actions:
        if col.button(label, key=f"{new_status}_{job['id']}", disabled=job["status"] == new_status, width="stretch"):
            storage.set_status(user, job["id"], new_status)
            st.rerun()

    st.link_button("Открыть объявление", job["url"], width="stretch")

    note = st.text_area("Заметка", job["note"] or "", key=f"note_{job['id']}", height=70,
                        placeholder="Контакт, дата отклика, впечатления…")
    if note != (job["note"] or ""):
        storage.set_note(user, job["id"], note)

    desc = job["description"]
    if desc is None and job["source"] == "Arbeitsagentur":
        if st.button("Загрузить описание"):
            text = sources.ba_description(job["ref"])
            if text is None:
                st.error("Arbeitsagentur не ответила. Попробуй ещё раз через минуту.")
            else:
                storage.set_description(job["id"], text)
                st.rerun()
    elif not desc:
        st.info("Описания нет: объявление снято или размещено на внешнем сайте. Открой ссылку выше.")
    else:
        safe = html.escape(desc)
        kw_re = sources.keyword_regex(kw_text.splitlines())
        if kw_re:
            safe = kw_re.sub(lambda m: f"<mark>{m.group(0)}</mark>", safe)
        safe = sources.LANGUAGE_HIGHLIGHT.sub(lambda m: f'<mark class="lang">{m.group(0)}</mark>', safe)
        st.markdown(f'<div class="job-desc">{safe.replace(chr(10), "<br>")}</div>', unsafe_allow_html=True)
