"""Два параллельных запроса проверки (ошибки без мышления + критерии с мышлением), К7–К10 по числу ошибок,
флаги обнуления, счёт слов по правилам ФИПИ. Сеть не вызывается — kimi/yavision/fipicache подменяются.
Запуск: python3 tests/test_essaycheck_split.py"""
import asyncio, json, os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", LOG_LEVEL="CRITICAL")
sys.path.insert(0, ROOT)
import essaycheck
def ok(c): print("  ✓", c)

# ---------- 1. счёт слов по правилам ФИПИ ----------
W = essaycheck._words
assert W("5 лет") == 1 and W("пять лет") == 2, (W("5 лет"), W("пять лет"))
assert W("всё-таки") == 1 and W("всё же") == 2
assert W("М.Ю. Лермонтов") == 1 and W("Н. В. Гоголь писал") == 2
assert W("и т.д.") == 1 and W("в 1812 году") == 2 and W("") == 0 and W(None) == 0
ok("_words(): цифры не считаются, инициалы с фамилией — одно слово, «т.д.» не слово, дефисные — одно")

# ---------- 2. К7–К10 считает код по числу ошибок ----------
essaycheck.MIN_WORDS = {"ege": 0, "oge": 0}
def err(t, n):
    return [{"type": t, "found": "слово%d" % i, "correct": "x", "why": "y", "src": "z"} for i in range(n)]
CR = [{"name": "К1 Отражение позиции автора", "got": 1, "max": 1, "why": "ок", "tip": ""},
      {"name": "К2 Комментарий к позиции", "got": 3, "max": 3, "why": "ок", "tip": ""},
      {"name": "К3 Собственное отношение", "got": 2, "max": 2, "why": "ок", "tip": ""},
      {"name": "К4 Фактическая точность", "got": 1, "max": 1, "why": "ок", "tip": ""},
      {"name": "К5 Логичность", "got": 2, "max": 2, "why": "ок", "tip": ""},
      {"name": "К6 Этические нормы", "got": 1, "max": 1, "why": "ок", "tip": ""}]
a = {"_lang": True, "criteria": CR, "recommendations": [],
     "errors": err("Пунктуация", 3) + err("Грамматика", 1) + err("Речь", 5)}
r = essaycheck._result(a, "текст работы", "задание", kind="ege")
by = {c["name"].split()[0]: c for c in r["criteria"]}
assert [by[k]["got"] for k in ("К7", "К8", "К9", "К10")] == [3, 1, 2, 0], by
assert r["score"]["max"] == 22 and r["score"]["got"] == 10 + 3 + 1 + 2 + 0, r["score"]
ok("К7–К10 по ошибкам: 0 → 3, 1–2 → 2, 3–4 → 1, 5+ → 0; тип «Грамматика» принят")

# пересборка из истории (без _lang) баллов не меняет
r2 = essaycheck._result(r, "текст работы", "задание", kind="ege")
assert r2["score"] == r["score"] and [c["got"] for c in r2["criteria"]] == [c["got"] for c in r["criteria"]]
ok("пересборка отчёта из истории не меняет баллы")

# блока ошибок нет — К7–К10 «нет данных», а не выдуманные 3 балла
a = {"_lang": True, "criteria": CR}
r = essaycheck._result(a, "текст", "задание", kind="ege")
assert [c["got"] for c in r["criteria"][6:]] == [0, 0, 0, 0] and "errors" in r["missing"], r
ok("нет блока ошибок → К7–К10 = 0 «нет данных», errors в missing")

# ---------- 3. флаги «работа не оценивается» ----------
a = {"_lang": True, "errors": [], "criteria": CR, "flags": {"copied": True, "reason": "пересказ"}}
r = essaycheck._result(a, "текст", "задание", kind="ege")
assert r["score"]["got"] == 0 and all(c["why"].startswith("По правилам ФИПИ") for c in r["criteria"])
r_again = essaycheck._result(r, "текст", "задание", kind="ege")
assert r_again["criteria"][0]["why"].count("По правилам ФИПИ") == 1, "пометка не должна удваиваться"
a = {"_lang": True, "errors": [], "criteria": CR, "flags": {"off_text": True}}
assert essaycheck._result(a, "текст", "", kind="ege")["score"]["got"] == 22 - 0, "без задания «не по тексту» не судим"
assert essaycheck._result(a, "текст", "задание", kind="ege")["score"]["got"] == 0
a = {"_lang": True, "errors": [], "criteria": CR, "flags": {"off_question": True}}
assert essaycheck._result(a, "текст", "задание", kind="ege")["score"]["got"] == 22, "off_question — только ОГЭ"
a = {"_lang": True, "errors": [], "criteria": CR, "flags": {"copied": "true"}}
assert essaycheck._result(a, "текст", "задание", kind="ege")["score"]["got"] == 22, "флаг — только строгий True"
ok("флаги: copied/off_text/off_question применяются кодом, строго True, без задания «не по тексту» не судим")

# ---------- 3б. без фото задания карточки по содержанию честно помечены ----------
NOTE = essaycheck.NOTASK_NOTE
a = {"_lang": True, "errors": err("Речь", 1), "criteria": CR}
r = essaycheck._result(a, "текст", "", kind="ege")
marked = [c["name"].split()[0] for c in r["criteria"] if c["why"].startswith(NOTE)]
assert marked == ["К1", "К2", "К3", "К4"], marked          # К5–К10 от исходного текста не зависят
assert not any(w in NOTE.lower() for w in ("задани", "услови", "опорн")), "пометка не должна резаться фильтром"
r_again = essaycheck._result(r, "текст", "", kind="ege")
assert r_again["criteria"][0]["why"].count(NOTE) == 1 and r_again["score"] == r["score"], "пометка не удваивается"
r_task = essaycheck._result(a, "текст", "задание есть", kind="ege")
assert not any(NOTE in c["why"] for c in r_task["criteria"]), "с заданием пометки нет"
OGE = [{"name": "СК1 Ответ на вопрос", "got": 1, "max": 1, "why": "ок", "tip": ""},
       {"name": "СК2 Примеры и пояснения", "got": 4, "max": 4, "why": "ок", "tip": ""},
       {"name": "СК3 Логичность", "got": 2, "max": 2, "why": "ок", "tip": ""},
       {"name": "СК4 Композиционная стройность", "got": 1, "max": 1, "why": "ок", "tip": ""}]
r = essaycheck._result({"errors": [], "criteria": OGE}, "текст", "", kind="oge")
assert [c["name"].split()[0] for c in r["criteria"] if c["why"].startswith(NOTE)] == ["СК1", "СК2"]
assert r["score"]["got"] == 8, "пометка баллы не меняет"
essaycheck.MIN_WORDS = {"ege": 150, "oge": 70}
r = essaycheck._result({"errors": [], "criteria": CR}, "мало слов", "", kind="ege")
assert not any(NOTE in c["why"] for c in r["criteria"]), "обнулённая по объёму работа пометки не получает"
essaycheck.MIN_WORDS = {"ege": 0, "oge": 0}
r = essaycheck._result({"errors": [], "criteria": CR, "flags": {"copied": True}}, "текст", "", kind="ege")
assert not any(NOTE in c["why"] for c in r["criteria"]), "обнулённая по флагу работа пометки не получает"
ok("без задания: К1–К4 (ОГЭ: СК1–СК2) помечены «оценка приблизительная», баллы и К5–К10 не тронуты, пометка идемпотентна")

# ---------- 4. run(): два параллельных запроса ----------
PAGES = [{"text": "Я считаю что книга важна.",
          "words": [{"t": t, "x1": i * 10, "y1": 5, "x2": i * 10 + 9, "y2": 10}
                    for i, t in enumerate(["Я", "считаю", "что", "книга", "важна."])]}]
ERR_JSON = json.dumps({"errors": [{"type": "Пунктуация", "found": "считаю что", "correct": "считаю, что",
                                   "why": "запятая", "src": "Розенталь, §1"}]})
CRIT_JSON = json.dumps({"flags": {"copied": False}, "criteria": CR, "recommendations": []})

def run_pipeline(fail_errors=False):
    calls = []
    async def fake_recognize(imgs):
        return PAGES
    async def fake_stream(messages, **kw):
        msg = messages[-1]["content"]
        is_err = "Найди ВСЕ ошибки" in msg
        calls.append({"err": is_err, "thinking": kw.get("thinking"), "cap": kw.get("max_tokens"),
                      "t": time.monotonic(), "msg": msg})
        await asyncio.sleep(0.15)
        if is_err and fail_errors:
            yield "err", "net"
            return
        if not is_err:
            yield "think", "думаю"
        yield "text", ERR_JSON if is_err else CRIT_JSON
    async def fake_criteria(kind, fallback=""):
        return fallback
    orig = (essaycheck.yavision.recognize_many, essaycheck.kimi.ask_stream, essaycheck.fipicache.criteria)
    essaycheck.yavision.recognize_many, essaycheck.kimi.ask_stream, essaycheck.fipicache.criteria = (
        fake_recognize, fake_stream, fake_criteria)
    async def go():
        t0 = time.monotonic()
        out = [ev async for ev in essaycheck.run(["w1"], [], "ege")]
        return out, time.monotonic() - t0
    try:
        out, dt = asyncio.run(go())
    finally:
        essaycheck.yavision.recognize_many, essaycheck.kimi.ask_stream, essaycheck.fipicache.criteria = orig
    return out, dt, calls

evs, dt, calls = run_pipeline()
assert len(calls) == 2, calls
ce = [c for c in calls if c["err"]][0]; cc = [c for c in calls if not c["err"]][0]
assert ce["thinking"] is False and ce["cap"] == essaycheck.EC_MAX_ERR, ce
assert cc["thinking"] is True and cc["cap"] == essaycheck.EC_MAX_CRIT, cc
assert "К1 Отражение" not in ce["msg"] and "К1 Отражение" in cc["msg"], "ошибки — без критериев, критерии — с таблицей"
assert abs(ce["t"] - cc["t"]) < 0.1 and dt < 0.28, "запросы должны идти параллельно (dt=%.2f)" % dt
ok("run(): ошибки (thinking=False, лимит %d) и критерии (thinking=True, лимит %d) идут параллельно, dt=%.2f с"
   % (essaycheck.EC_MAX_ERR, essaycheck.EC_MAX_CRIT, dt))
kinds = [k for k, _ in evs]
assert kinds.count("think") == 1 and kinds[-1] == "done"
r = [v for k, v in evs if k == "done"][0]
assert r["errors"][0]["found"] == "считаю что" and "x" in r["errors"][0], r["errors"]
assert [c["got"] for c in r["criteria"][6:]] == [3, 2, 3, 3] and r["why"] == "", r
ok("run(): итог собран из двух ответов, К8 = 2 по одной пунктуационной ошибке, координаты из OCR")

# один из запросов упал — второй блок не теряется
evs, dt, calls = run_pipeline(fail_errors=True)
r = [v for k, v in evs if k == "done"][0]
assert r["why"] == "part" and "errors" in r["missing"] and r["criteria"][0]["got"] == 1, r
ok("run(): ошибки не получены → критерии показаны, errors в missing, why=part")

print("test_essaycheck_split: все проверки прошли")
