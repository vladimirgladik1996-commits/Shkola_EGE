"""ИИ-проверка БЕЗ фото задания: ничего не выдумывается (баг-репорт 006).
Сеть не вызывается — yavision/kimi/fipicache подменяются. Проверяются:
  • _no_task() — детерминированный фильтр выдумок о задании/условии/опорном тексте;
  • _result() — без task выдумки вырезаются, с task остаются как есть;
  • _bind_coords()+_result() — ошибки без подтверждения в тексте отбрасываются;
  • run() — этапы идут параллельно; «только фото задания», «фото без текста»,
    сбой части фото (why="part"); промт без задания — с TASK_MISSING.
Запуск: python3 tests/test_essaycheck_notask.py"""
import asyncio, json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.update(BOT_TOKEN="1:x", LOG_LEVEL="CRITICAL")
import essaycheck, kimi, yavision

def ok(c): print("  ✓", c)

TEXT = "Автор поднимает проблему влеяния книги. Я считаю что книга важна."
PAGE = {"text": TEXT,
        "words": [{"t": "влеяния", "x1": 40, "y1": 5, "x2": 55, "y2": 10},
                  {"t": "считаю", "x1": 12, "y1": 20, "x2": 25, "y2": 25},
                  {"t": "что", "x1": 27, "y1": 20, "x2": 35, "y2": 25}]}
PAGE_T = {"text": "Условие: по тексту…", "words": [{"t": "Условие:", "x1": 5, "y1": 5, "x2": 20, "y2": 10}]}

# ---------- 1. _no_task(): предложения со ссылками на задание вырезаются целиком ----------
s = essaycheck._no_task("Работа читается. По условию задания тема раскрыта. Ошибок мало.")
assert "услови" not in s.lower() and "задани" not in s.lower(), s
assert "Работа читается." in s and "Ошибок мало." in s, s
assert essaycheck._no_task("Проверьте орфографию.") == "Проверьте орфографию."
assert essaycheck._no_task("Опорный текст важен. Перепишите.") == "Перепишите."
assert essaycheck._no_task("") == "" and essaycheck._no_task(None) == ""
ok("_no_task(): выдумки о задании/условии/опорном тексте вырезаются, остальное остаётся")

# ---------- 2. _result(): без task — чистка выдумок; с task — текст модели не трогается ----------
a = {"errors": [{"type": "Орфография", "found": "влеяния", "correct": "влияния",
                 "why": "Ошибка в корне. По условию задания не раскрыта тема.", "src": "Розенталь, §…"}],
     "criteria": [{"name": "К1", "got": 1, "max": 1,
                   "why": "Тема видна в тексте. Условие задания не учтено.", "tip": "Перечитайте задание."}],
     "recommendations": [{"title": "Работа над ошибками", "text": "Уточните условие задания у учителя.", "badge": "+1"}]}
r = essaycheck._result(a, TEXT, "")
assert "задани" not in r["errors"][0]["why"].lower() and "услови" not in r["errors"][0]["why"].lower(), r["errors"][0]
assert "Ошибка в корне." in r["errors"][0]["why"] and r["errors"][0]["src"]
assert "задани" not in r["criteria"][0]["why"].lower() and r["criteria"][0]["tip"] == ""
assert r["recommendations"][0]["text"] == "" and r["recommendations"][0]["title"] == "Работа над ошибками"
r2 = essaycheck._result(a, TEXT, "Условие: по тексту…")
assert "задани" in r2["errors"][0]["why"].lower() and r2["criteria"][0]["tip"] != ""
ok("_result(): без задания выдумки вырезаны, с заданием — модель не фильтруется")

# ---------- 3. выдуманные ошибки (found нет в тексте) отбрасываются ----------
an = {"errors": [{"type": "Орфография", "found": "влеяния", "correct": "влияния", "why": "проверка гласной", "src": "Розенталь"},
                 {"type": "Речь", "found": "абракадабра", "correct": "…", "why": "выдумка", "src": "…"}],
      "criteria": [{"name": "К1", "got": 1, "max": 1, "why": "ок", "tip": ""}]}
essaycheck._bind_coords(an, [PAGE])
r = essaycheck._result(an, TEXT, "")
assert [e["found"] for e in r["errors"]] == ["влеяния"], r["errors"]
assert "errors" not in r["missing"]
ok("_bind_coords()+_result(): ошибка без фрагмента в тексте не показывается")

# ---------- 4. ни одна ошибка не подтвердилась → блок errors пустой, а не «0 ошибок» ----------
an = {"errors": [{"type": "Речь", "found": "абракадабра", "correct": "…", "why": "выдумка", "src": "…"}],
      "criteria": [{"name": "К1", "got": 1, "max": 1, "why": "ок", "tip": ""}]}
essaycheck._bind_coords(an, [PAGE])
r = essaycheck._result(an, TEXT, "")
assert r["errors"] == [] and "errors" in r["missing"], (r["errors"], r["missing"])
ok("все ошибки выдуманы → errors в missing (фронт покажет блок пустым, без «0 ошибок»)")

# ---------- 5. run(): этапы идут ПАРАЛЛЕЛЬНО (OCR работы ∥ OCR задания ∥ критерии ФИПИ) ----------
def run_pipeline():
    real_rec, real_stream, real_json = yavision.recognize_many, kimi.ask_stream, kimi.ask_json
    marks = []

    async def slow_recognize(imgs):
        marks.append(("ocr", time.monotonic(), "s"))
        await asyncio.sleep(0.25)
        marks.append(("ocr", time.monotonic(), "e"))
        return [PAGE] if imgs == ["w1"] else [PAGE_T]

    async def slow_criteria(kind, fallback=""):
        marks.append(("crit", time.monotonic(), "s"))
        await asyncio.sleep(0.25)
        marks.append(("crit", time.monotonic(), "e"))
        return ""

    async def fake_stream(msgs, **kw):
        yield "text", '{"errors":[],"criteria":[{"name":"К1","got":1,"max":1,"why":"ок","tip":""}],"recommendations":[]}'

    yavision.recognize_many, kimi.ask_stream = slow_recognize, fake_stream
    essaycheck.fipicache.criteria = slow_criteria
    t0 = time.monotonic()
    evs = asyncio.run(_collect(essaycheck.run(["w1"], ["t1"], "ege")))
    el = time.monotonic() - t0
    starts = sorted(ms for _, ms, k in marks if k == "s")
    ends = sorted(ms for _, ms, k in marks if k == "e")
    assert len(starts) == 3 and len(ends) == 3, marks
    assert el < 0.6, el            # три этапа по 0.25 с уложились параллельно, а не в 0.75 с
    assert max(starts) < min(ends), (starts, ends)   # все три шли одновременно
    assert evs[-1][0] == "done" and evs[-1][1]["why"] == "", evs[-1]
    ok("run(): OCR работы, OCR задания и критерии ФИПИ идут параллельно (3×0.25 с → <0.6 с)")

    # ---------- 6. только фото задания (без работы) → превью с прочитанным заданием, без выдумок ----------
    async def only_task(imgs):
        return [] if not imgs else [PAGE_T]
    yavision.recognize_many = only_task
    evs = asyncio.run(_collect(essaycheck.run([], ["t1"], "ege")))
    res = evs[-1][1]
    assert evs[-1][0] == "done" and res["why"] == "empty" and "text" in res["missing"], res
    assert res["task"] == PAGE_T["text"] and "criteria" in res["missing"]
    ok("run(): только фото задания → done с прочитанным заданием, текст работы — в missing, ничего не выдумано")

    # ---------- 7. фото без текста (например, здание) → честное превью, не ошибка ----------
    async def no_text(imgs):
        return [{"text": "", "words": []}]
    yavision.recognize_many = no_text
    evs = asyncio.run(_collect(essaycheck.run(["w1"], [], "ege")))
    res = evs[-1][1]
    assert evs[-1][0] == "done" and res["why"] == "empty" and "text" in res["missing"], res
    assert res["errors"] == [] and res["criteria"] == []
    ok("run(): фото без текста → превью с пустыми блоками и why=empty (не зависание, не выдумки)")

    # ---------- 8. сбой части фото → why=\"part\", проверка не роняется ----------
    async def partial(imgs):
        return [PAGE, {"text": "", "words": [], "failed": True}] if imgs == ["w1"] else [PAGE_T]
    yavision.recognize_many = partial
    evs = asyncio.run(_collect(essaycheck.run(["w1"], ["t1"], "ege")))
    res = evs[-1][1]
    assert evs[-1][0] == "done" and res["why"] == "part" and res["text"] == TEXT, res
    ok("run(): часть фото не распозналась → итог честно помечен why=part, остальное разобрано")

    # ---------- 9. без задания: промт с TASK_MISSING, в ответе нет ни слова о задании ----------
    seen = {}

    async def rec2(imgs):
        return [PAGE] if imgs == ["w1"] else ([PAGE_T] if imgs else [])

    async def stream2(msgs, **kw):
        seen["msg"] = msgs[0]["content"]
        yield "text", ('{"errors":[{"type":"Орфография","found":"влеяния","correct":"влияния",'
                       '"why":"проверка гласной. В условии задания сказано раскрыть тему.","src":"Розенталь"}],'
                       '"criteria":[{"name":"К1","got":1,"max":1,"why":"ок","tip":"Сверьте с заданием."}],'
                       '"recommendations":[{"title":"Совет","text":"Прочитайте условие задания.","badge":"+1"}]}')

    yavision.recognize_many, kimi.ask_stream = rec2, stream2
    evs = asyncio.run(_collect(essaycheck.run(["w1"], [], "ege")))
    res = evs[-1][1]
    assert essaycheck.TASK_MISSING in seen["msg"], "в промте нет TASK_MISSING"
    blob = json.dumps(res, ensure_ascii=False).lower()
    assert "задани" not in blob and "услови" not in blob and "опорн" not in blob, blob
    assert res["errors"] and res["errors"][0]["why"] == "проверка гласной."
    assert res["criteria"][0]["tip"] == "" and res["recommendations"][0]["text"] == ""
    ok("run() без задания: TASK_MISSING в промте + в разборе нет НИ ОДНОГО упоминания задания/условия")

    # ---------- 10. с заданием упоминания задания законны и остаются ----------
    evs = asyncio.run(_collect(essaycheck.run(["w1"], ["t1"], "ege")))
    res = evs[-1][1]
    blob = json.dumps(res, ensure_ascii=False).lower()
    assert "услови" in res["task"].lower() and "задани" in blob
    ok("run() с заданием: упоминания задания сохраняются (фильтр не мешает полноценному разбору)")

    yavision.recognize_many, kimi.ask_stream, kimi.ask_json = real_rec, real_stream, real_json
    essaycheck.fipicache.criteria = real_criteria

real_criteria = essaycheck.fipicache.criteria

async def _collect(gen):
    return [e async for e in gen]

run_pipeline()
print("test_essaycheck_notask: все проверки прошли")
