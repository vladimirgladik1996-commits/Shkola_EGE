"""essaycheck (боевая ветка): привязка координат ошибок к словам OCR, промты, разбор JSON.
Сеть не вызывается — kimi/yavision подменяются; проверяются _bind_coords/_msg/_parse_json/_result."""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", LOG_LEVEL="CRITICAL")
os.environ.pop("EC_LIVE", None)
sys.path.insert(0, ROOT)
import essaycheck
def ok(c): print("  ✓", c)

def words(*specs):   # (текст, x1, y1, x2, y2)
    return [{"t": t, "x1": a, "y1": b, "x2": c, "y2": d} for t, a, b, c, d in specs]

PAGES = [{"text": "Я считаю что книга важна. Во первых она учит.",
          "words": words(("Я", 5, 5, 10, 10), ("считаю", 12, 5, 25, 10), ("что", 27, 5, 35, 10),
                         ("книга", 37, 5, 50, 10), ("важна.", 52, 5, 65, 10),
                         ("Во", 5, 20, 10, 25), ("первых", 12, 20, 25, 25), ("она", 27, 20, 35, 25),
                         ("учит.", 37, 20, 50, 25))},
         {"text": "Ещё мысль.",
          "words": words(("Ещё", 5, 5, 15, 10), ("мысль.", 17, 5, 30, 10))}]

# ---------- 1. привязка координат: точное совпадение ----------
a = {"errors": [{"found": "считаю что"}, {"found": "книга"}]}
essaycheck._bind_coords(a, PAGES)
e1, e2 = a["errors"]
assert e1["page"] == 0 and abs(e1["x"] - 23.5) < 0.2 and abs(e1["y"] - 7.5) < 0.2, e1
assert e2["page"] == 0 and abs(e2["x"] - 43.5) < 0.2, e2
ok("_bind_coords(): фрагмент из 2 слов → центр объединённого бокса, page — индекс фото")

# ---------- 2. ё/е, регистр, пунктуация ----------
a = {"errors": [{"found": "ЕЩЁ мысль"}, {"found": "важна"}, {"found": "считаю, что"}]}
essaycheck._bind_coords(a, PAGES)
assert a["errors"][0]["page"] == 1, a["errors"][0]
assert a["errors"][1]["page"] == 0 and abs(a["errors"][1]["x"] - 58.5) < 0.2
assert a["errors"][2]["page"] == 0              # запятая в found не мешает поиску
ok("_bind_coords(): ё/е, регистр и пунктуация в found нормализуются")

# ---------- 3. нечёткое совпадение (расхождение ≤1 символа) и полный промах ----------
a = {"errors": [{"found": "во первых"},          # fuzzy: «Во первых» ≈ «во первых» (и «первых» ≈ «первых»)
                {"found": "во-первых"},          # то же через дефис
                {"found": "абракадабра"}]}       # такого в тексте нет
essaycheck._bind_coords(a, PAGES)
assert a["errors"][0]["page"] == 0 and "x" in a["errors"][0]
assert a["errors"][1]["page"] == 0 and "x" in a["errors"][1]
assert "x" not in a["errors"][2] and "page" not in a["errors"][2]
ok("_bind_coords(): нечёткое совпадение ≤1 символа; промах — без координат (оверлея нет)")

# ---------- 4. модель прислала координаты — они затираются разметкой OCR ----------
a = {"errors": [{"found": "книга", "page": 9, "x": 99, "y": 99}]}
essaycheck._bind_coords(a, PAGES)
assert a["errors"][0]["page"] == 0 and abs(a["errors"][0]["x"] - 43.5) < 0.2
ok("_bind_coords(): координаты модели перезаписываются координатами OCR")

# ---------- 5. промты: ЕГЭ/ОГЭ и task_missing ----------
m = essaycheck._msg("ege", "", essaycheck.DEMO_TEXT, essaycheck.CRITERIA["ege"])
assert "сочинение ЕГЭ" in m and essaycheck.TASK_MISSING in m and essaycheck.DEMO_TEXT in m
m = essaycheck._msg("oge", "Опорный текст: …", essaycheck.DEMO_TEXT, essaycheck.CRITERIA["oge"])
assert "эссе для ОГЭ" in m and "Опорный текст: …" in m and essaycheck.TASK_MISSING not in m
assert "НЕ ВЫДУМЫВАЙ" in m or "не придумывай" in m.lower()
ok("_msg(): контекст ЕГЭ/ОГЭ, блок задания или запрет выдумывать, текст работы в промте")

# ---------- 6. разбор JSON модели (с мусором и без) ----------
assert essaycheck._parse_json('{"a": 1}') == {"a": 1}
assert essaycheck._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
assert essaycheck._parse_json('Вот разбор: {"errors": []} — конец') == {"errors": []}
assert essaycheck._parse_json("") is None and essaycheck._parse_json("просто текст") is None
assert essaycheck._parse_json("[1,2]") is None
ok("_parse_json(): JSON из ответа модели извлекается даже с посторонним текстом")

# ---------- 7. пустой src → шаблон (проверяем через боевой путь run с подменёнными API) ----------
def run_pipeline():
    async def fake_recognize(imgs):
        return PAGES[:1] if len(imgs) == 1 and imgs == ["w1"] else PAGES[1:]
    async def fake_stream(messages, **kw):
        yield "think", "думаю"
        yield "text", ('{"errors":[{"type":"Пунктуация","found":"считаю что","correct":"считаю, что",'
                       '"why":"запятая перед «что»","src":""}],"criteria":[{"name":"К1","got":1,"max":1,'
                       '"why":"ок","tip":""}],"recommendations":[]}')
    async def fake_criteria(kind, fallback=""):
        return fallback
    orig = (essaycheck.yavision.recognize_many, essaycheck.kimi.ask_stream,
            essaycheck.fipicache.criteria, essaycheck.DEMO)
    essaycheck.yavision.recognize_many = fake_recognize
    essaycheck.kimi.ask_stream = fake_stream
    essaycheck.fipicache.criteria = fake_criteria
    essaycheck.DEMO = False
    async def go():
        out = []
        async for ev in essaycheck.run(["w1"], ["t1"], "oge"):
            out.append(ev)
        return out
    try:
        return asyncio.run(go())
    finally:
        (essaycheck.yavision.recognize_many, essaycheck.kimi.ask_stream,
         essaycheck.fipicache.criteria, essaycheck.DEMO) = orig

evs = run_pipeline()
kinds = [k for k, _ in evs]
assert kinds.count("think") == 1 and kinds[-1] == "done", kinds
assert ("status", "Распознан рукописный текст") in evs
assert ("status", "Прочитано задание и опорный текст") in evs
r = [v for k, v in evs if k == "done"][0]
e = r["errors"][0]
assert e["src"] == essaycheck.SRC_TEMPLATE, e
assert e["page"] == 0 and abs(e["x"] - 23.5) < 0.2
assert r["score"] == {"got": 1, "max": 1, "percent": 100, "level": "Отлично"} or r["score"]["got"] == 1
ok("run() боевой путь: status → think → done, пустой src → шаблон, координаты из OCR, балл посчитан кодом")

print("test_essaycheck_live: все проверки прошли")
