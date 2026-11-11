"""Перепроверка рукописи (ocrverify): два прогона Yandex → спорные слова → Kimi по вырезкам.
Сеть не вызывается: kimi/yavision подменяются. Запуск: python3 tests/test_ocrverify.py"""
import asyncio, base64, io, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", LOG_LEVEL="CRITICAL")
sys.path.insert(0, ROOT)
import essaycheck, kimi, ocrverify, yavision
from PIL import Image

essaycheck.MIN_WORDS = {"ege": 0, "oge": 0}
def ok(c): print("  ✓", c)

def W(t, x1, y1, x2, y2, s=None, n=None):
    e = {"t": t, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "li": 0}
    if s is not None:
        e["s"], e["n"] = s, n
    return e

def lw_from(text, specs, seg=True):
    """specs: [(слово, x1, x2)] в одной строке (y 10–20); позиции s/n считаются по тексту."""
    out, cur = [], 0
    for t, x1, x2 in specs:
        p = text.find(t, cur)
        out.append(W(t, x1, 10, x2, 20, p if seg else None, len(t) if seg else None))
        cur = p + len(t)
    return out

def white_b64(w=1000, h=600):
    b = io.BytesIO(); Image.new("RGB", (w, h), (250, 250, 250)).save(b, "JPEG"); return base64.b64encode(b.getvalue()).decode()

# ---------- 1. yavision.parse_lw: слова внутри строк + позиция в fullText ----------
def verts(x1, y1, x2, y2): return {"vertices": [{"x": str(x1), "y": str(y1)}, {"x": str(x2), "y": str(y1)},
                                                {"x": str(x2), "y": str(y2)}, {"x": str(x1), "y": str(y2)}]}
J = {"result": {"textAnnotation": {"width": "1000", "height": "500", "fullText": "Привет мир", "blocks": [{"lines": [
    {"text": "Привет мир", "boundingBox": verts(100, 50, 500, 100), "words": [
        {"text": "Привет", "boundingBox": verts(100, 50, 300, 100), "textSegments": [{"startIndex": "0", "length": "6"}]},
        {"text": "мир", "boundingBox": verts(320, 50, 500, 100), "textSegments": [{"startIndex": "7", "length": "3"}]}]}]}]}}}
lw = yavision.parse_lw(J)
assert [w["t"] for w in lw] == ["Привет", "мир"] and lw[0]["s"] == 0 and lw[0]["n"] == 6 and lw[1]["s"] == 7, lw
assert abs(lw[0]["x1"] - 10) < 0.2 and abs(lw[0]["y2"] - 20) < 0.2 and lw[0]["li"] == lw[1]["li"] == 0
assert yavision.parse_lw({}) == [] and yavision.parse_lw(None) == [] and yavision.parse_lw({"result": {"textAnnotation": {"blocks": "мусор"}}}) == []
assert yavision.parse(J)["words"] and "lw" not in yavision.parse(J)       # parse() не меняется
ok("parse_lw(): только слова из строк, li/s/n, толерантен к мусору; parse() не тронут")

# ---------- 2. compare(): сопоставление двух прогонов ----------
T1 = "Я люблю празник и книги"
hw = lw_from(T1, [("Я", 5, 8), ("люблю", 10, 25), ("празник", 27, 45), ("и", 47, 50), ("книги", 52, 70)])
pg = [W("Я", 5, 10, 8, 20), W("люблю", 10, 10, 25, 20), W("праздник", 27, 10, 46, 20), W("и", 47, 10, 50, 20),
      W("книги", 52, 10, 70, 20), W(",", 71, 10, 72, 20)]
d = ocrverify.compare([{"text": T1, "lw": hw}], [{"text": "x", "lw": pg}])
assert len(d) == 1 and d[0]["a"] == "празник" and d[0]["b"] == "праздник" and d[0]["d"] == 1 and d[0]["page"] == 0, d
ok("compare(): одинаковые слова (в т.ч. ё/е, регистр, знаки) — надёжные; расхождение — спорное слово с A/B")

pg_split = [W("пере", 5, 10, 12, 20), W("несённых", 13, 10, 25, 20)]
assert ocrverify.compare([{"text": "t", "lw": [W("перенесённых", 5, 10, 25, 20)]}], [{"text": "t", "lw": pg_split}]) == []
ok("compare(): слово разбито page на части, но буквы те же — не спорное")

merged = [W("люблю празник", 10, 10, 45, 20)]                          # page «накрыл» два слова handwritten
hw2 = [W("люблю", 10, 10, 25, 20), W("празник", 27, 10, 45, 20)]
assert ocrverify.compare([{"text": "t", "lw": hw2}], [{"text": "t", "lw": merged}]) == []
assert ocrverify.compare([{"text": "t", "lw": hw2}], [{"text": "t", "lw": [W("люблю", 10, 50, 25, 60)]}]) == []
ok("compare(): неоднозначное соответствие или нет пары на том же месте — слово не сравнивается")

assert ocrverify.compare([{"text": "t", "failed": True}], [{"text": "t", "lw": pg}]) == []
assert ocrverify.compare([{"text": "t"}], [{"text": "t", "lw": pg}]) == [] and ocrverify.compare(None, None) == []
ok("compare(): упавшие страницы и страницы без lw пропускаются")

hwm = [W("аааа", 5, 10, 10, 20), W("бббб", 12, 10, 17, 20), W("вввв", 19, 10, 24, 20)]
pgm = [W("аабб", 5, 10, 10, 20), W("бббб", 12, 10, 17, 20), W("вввб", 19, 10, 24, 20)]      # d=2, d=0, d=1
assert [x["a"] for x in ocrverify.compare([{"text": "t", "lw": hwm}], [{"text": "t", "lw": pgm}])] == ["вввв", "аааа"]
old_max = ocrverify.MAX_WORDS; ocrverify.MAX_WORDS = 1
assert [x["a"] for x in ocrverify.compare([{"text": "t", "lw": hwm}], [{"text": "t", "lw": pgm}])] == ["вввв"]
ocrverify.MAX_WORDS = old_max
ok("compare(): близкие прочтения — первыми, число слов ограничено MAX_WORDS")

# ---------- 3. resolve(): решения Кими и применение правок ----------
class FakeKimi:
    def __init__(self, fn): self.fn, self.calls = fn, []
    async def __call__(self, messages, images=None, max_tokens=None, **kw):
        self.calls.append((messages[-1]["content"], images))
        r = self.fn(messages[-1]["content"], images)
        if isinstance(r, Exception): raise r
        return r

def run_resolve(pages, doubts, fn, images=None):
    fk = FakeKimi(fn); orig = kimi.ask_json; kimi.ask_json = fk
    try:
        st = asyncio.run(ocrverify.resolve(images or [white_b64()] * len(pages), pages, doubts))
    finally:
        kimi.ask_json = orig
    return st, fk

def mkpage(text, specs, seg=True, with_words=True):
    lw_ = lw_from(text, specs, seg)
    words = ([dict(w) for w in lw_] + [{"t": text, "x1": 5, "y1": 10, "x2": 70, "y2": 20}]) if with_words else []
    return {"text": text, "lw": lw_, "words": words}

def DB(a, b, wi, page=0, box=None):
    return {"page": page, "wi": wi, "a": a, "b": b, "d": 1, "box": box or {"x1": 27, "y1": 10, "x2": 45, "y2": 20}}

SPECS = [("Я", 5, 8), ("люблю", 10, 25), ("празник", 27, 45), ("и", 47, 50), ("книги.", 52, 70)]
P = mkpage(T1 + ".", SPECS)
st, fk = run_resolve([P], [DB("празник", "праздник", 2)], lambda m, im: {"r": [{"i": 1, "pick": "B", "read": "праздник", "sure": True}]})
assert P["text"] == "Я люблю праздник и книги." and st == {"doubts": 1, "asked": 1, "fixed": 1}, (P["text"], st)
assert P["lw"][2]["t"] == "праздник" and P["vfix"] == [{"from": "празник", "to": "праздник"}]
assert [w["t"] for w in P["words"] if w["t"] != P["text"]][2] == "праздник"
assert any(w["t"] == "Я люблю праздник и книги." for w in P["words"])     # строка в «words» (для привязки ошибок к фото) тоже обновлена
assert len(fk.calls) == 1 and len(fk.calls[0][1]) == 1 and fk.calls[0][1][0]                  # одна вырезка (jpeg base64)
assert 'A="празник" B="праздник"' in fk.calls[0][0] and "БУКВА В БУКВУ" in fk.calls[0][0] and "НЕ исправляй" in fk.calls[0][0]
Image.open(io.BytesIO(base64.b64decode(fk.calls[0][1][0]))).verify()
ok("resolve(): уверенный выбор B → слово заменено в тексте, lw и words; вырезка — валидный JPEG; промт запрещает «исправлять»")

def one(a, b, ans, text=None, specs=None, wi=2):
    Pg = mkpage(text or T1 + ".", specs or SPECS)
    st, _ = run_resolve([Pg], [DB(a, b, wi)], lambda m, im: {"r": [ans]})
    return Pg["text"], st["fixed"]

BASE = "Я люблю празник и книги."
READ = ["праздник"]
assert one("празник", "праздник", {"i": 1, "pick": "B", "sure": False}) == (BASE, 0)
assert one("празник", "праздник", {"i": 1, "pick": "A", "sure": True}) == (BASE, 0)
assert one("празник", "праздник", {"i": 1, "pick": "?", "sure": True}) == (BASE, 0)
assert one("празник", "праздник", {"i": 1, "pick": "B"}) == (BASE, 0)
assert one("празник", "праздник", {"i": 1, "pick": "B", "read": "", "sure": True}) == (BASE, 0)            # B без собственного прочтения
assert one("празник", "праздник", {"i": 1, "pick": "B", "read": "празник", "sure": True}) == (BASE, 0)    # выбрала B, а читает A — противоречие
assert one("празник", "праздник", {"i": 1, "pick": "X", "read": "пра́здник", "sure": "да"}) == (BASE, 0)   # sure не булево true
ok("resolve(): без sure=true, при выборе A или «?» текст не меняется (защита от «исправления» ошибок ученика)")

assert one("празник", "праздник", {"i": 1, "pick": "X", "read": "празднык", "sure": True})[0] == "Я люблю празднык и книги."
assert one("празник", "праздник", {"i": 1, "pick": "X", "read": "карусель", "sure": True}) == (BASE, 0)      # далеко от A и B
assert one("празник", "праздник", {"i": 1, "pick": "X", "read": "х", "sure": True}) == (BASE, 0)
ok("resolve(): «иначе» принимается только рядом (≤3 правок) с одним из прочтений — выдумывание слов исключено")

t2, f2 = one("Празник", "праздник", {"i": 1, "pick": "B", "read": "праздник", "sure": True}, text="Празник и книги.",
             specs=[("Празник", 0, 20), ("и", 22, 25), ("книги.", 27, 45)], wi=0)
assert (t2, f2) == ("Праздник и книги.", 1), t2
t3, _ = one("ПРАЗНИК", "праздник", {"i": 1, "pick": "B", "read": "праздник", "sure": True}, text="ПРАЗНИК и книги.",
            specs=[("ПРАЗНИК", 0, 20), ("и", 22, 25), ("книги.", 27, 45)], wi=0)
assert t3 == "ПРАЗДНИК и книги.", t3
t4, _ = one("празник,", "праздник", {"i": 1, "pick": "B", "read": "праздник", "sure": True}, text="Я люблю празник, и книги.",
            specs=[("Я", 5, 8), ("люблю", 10, 25), ("празник,", 27, 45), ("и", 47, 50), ("книги.", 52, 70)])
assert t4 == "Я люблю праздник, и книги.", t4
ok("resolve(): регистр (Первая/ВСЕ) и знаки препинания у слова сохраняются")

Pn = mkpage(T1 + ".", SPECS, seg=False)                                   # API не дал textSegments → поиск по порядку чтения
run_resolve([Pn], [DB("празник", "праздник", 2)], lambda m, im: {"r": [{"i": 1, "pick": "B", "read": READ[0], "sure": True}]})
assert Pn["text"] == "Я люблю праздник и книги.", Pn["text"]
Pw = mkpage("и пол и пол и", [("и", 1, 3), ("пол", 4, 8), ("и", 9, 11), ("пол", 12, 16), ("и", 17, 19)], seg=False)
run_resolve([Pw], [DB("пол", "пал", 3, box={"x1": 12, "y1": 10, "x2": 16, "y2": 20})], lambda m, im: {"r": [{"i": 1, "pick": "B", "read": "пал", "sure": True}]})
assert Pw["text"] == "и пол и пал и", Pw["text"]                         # заменено именно ВТОРОЕ «пол»
Pbad = mkpage(T1 + ".", SPECS); Pbad["lw"][2]["s"] = 3                    # s/n не совпали с текстом → не доверяем, ищем по порядку
run_resolve([Pbad], [DB("празник", "праздник", 2)], lambda m, im: {"r": [{"i": 1, "pick": "B", "read": READ[0], "sure": True}]})
assert Pbad["text"] == "Я люблю праздник и книги.", Pbad["text"]
ok("resolve(): позиция слова — из textSegments, иначе по порядку чтения; повторяющиеся слова не путаются")

# --- сбои: текст остаётся как дал Яндекс ---
for label, fn in (("None", lambda m, im: None), ("исключение", lambda m, im: RuntimeError("boom")),
                  ("мусор", lambda m, im: {"r": "не список"}), ("чужие индексы", lambda m, im: {"r": [{"i": 7, "pick": "B", "sure": True}, {"i": "x"}, 5]})):
    Pf = mkpage(T1 + ".", SPECS)
    st, _ = run_resolve([Pf], [DB("празник", "праздник", 2)], fn)
    assert Pf["text"] == "Я люблю празник и книги." and st["fixed"] == 0 and "vfix" not in Pf, (label, Pf["text"])
ok("resolve(): None/исключение/мусор в ответе Кими → текст не тронут")

Pb = mkpage(T1 + ".", SPECS)
st, fk = run_resolve([Pb], [DB("празник", "праздник", 2)], lambda m, im: {"r": [{"i": 1, "pick": "B", "read": READ[0], "sure": True}]}, images=["не-картинка"])
assert Pb["text"] == "Я люблю празник и книги." and st["asked"] == 0 and not fk.calls
ok("resolve(): страница не открылась для вырезок → Кими не вызывается, текст не тронут")

# --- чанки: индексы ответов сопоставляются с нужными словами ---
words5 = ["аб", "вг", "де", "жз", "ик"]
text5 = " ".join(words5)
P5 = mkpage(text5, [(w, 5 + i * 12, 15 + i * 12) for i, w in enumerate(words5)])
D5 = [DB(w, w + "я", i, box={"x1": 5 + i * 12, "y1": 10, "x2": 15 + i * 12, "y2": 20}) for i, w in enumerate(words5)]
def answer_all_b(msg, im):
    n = len(re.findall(r"^#\d+:", msg, re.M))
    bs = re.findall(r'^#\d+: A="[^"]*" B="([^"]*)"', msg, re.M)
    return {"r": [{"i": k, "pick": "B", "read": bs[k - 1], "sure": True} for k in range(1, n + 1)]}
old_chunk = ocrverify.CHUNK; ocrverify.CHUNK = 2
st, fk = run_resolve([P5], D5, answer_all_b)
ocrverify.CHUNK = old_chunk
assert len(fk.calls) == 3 and st["fixed"] == 5 and P5["text"] == "абя вгя дея жзя икя", (len(fk.calls), P5["text"])
ok("resolve(): вырезки уходят пачками (CHUNK), ответы возвращаются к своим словам")

# ---------- 4. enabled() ----------
orig = (yavision.API_KEY, kimi.API_KEY, os.environ.get("EC_WORD_VERIFY"))
yavision.API_KEY = kimi.API_KEY = "k"; os.environ.pop("EC_WORD_VERIFY", None)
assert ocrverify.enabled() is True
os.environ["EC_WORD_VERIFY"] = "0"; assert ocrverify.enabled() is False
os.environ.pop("EC_WORD_VERIFY"); kimi.API_KEY = ""; assert ocrverify.enabled() is False
kimi.API_KEY = "k"; yavision.API_KEY = ""; assert ocrverify.enabled() is False
yavision.API_KEY, kimi.API_KEY = orig[0], orig[1]
if orig[2] is not None: os.environ["EC_WORD_VERIFY"] = orig[2]
ok("enabled(): нужны оба ключа, выключатель EC_WORD_VERIFY=0 работает")

# ---------- 5. сквозной прогон essaycheck.run ----------
TXT = "Я считаю что книга празник важна."
S = [("Я", 5, 8), ("считаю", 10, 25), ("что", 27, 35), ("книга", 37, 50), ("празник", 52, 70), ("важна.", 72, 85)]
HW_PAGE = {"text": TXT, "lw": lw_from(TXT, S), "words": lw_from(TXT, S)}
PG_PAGE = {"text": "x", "lw": [dict(w) for w in lw_from(TXT, S)]}
PG_PAGE["lw"][4]["t"] = "праздник"

def run_pipeline(page2_fn, kimi_fn, keys=True, recognize=None):
    state = {"models": []}
    async def fake_recognize(imgs, model="handwritten", **kw):
        state["models"].append(model)
        return page2_fn(model)
    async def fake_stream(messages, **kw):
        yield "think", "думаю"
        yield "text", '{"errors":[{"type":"Орфография","found":"празник","correct":"праздник","why":"НН","src":""}],"criteria":[{"name":"СК1","got":1,"max":1,"why":"ок","tip":""}],"recommendations":[]}'
    async def fake_criteria(kind, fallback=""): return fallback
    fk = FakeKimi(kimi_fn)
    orig = (yavision.recognize_many, kimi.ask_stream, kimi.ask_json, essaycheck.fipicache.criteria, yavision.API_KEY, kimi.API_KEY)
    yavision.recognize_many, kimi.ask_stream, kimi.ask_json, essaycheck.fipicache.criteria = (recognize or fake_recognize), fake_stream, fk, fake_criteria
    yavision.API_KEY, kimi.API_KEY = ("y", "k") if keys else ("", "")
    async def go():
        return [ev async for ev in essaycheck.run([white_b64()], [], "oge")]
    try:
        return asyncio.run(go()), state, fk
    finally:
        (yavision.recognize_many, kimi.ask_stream, kimi.ask_json, essaycheck.fipicache.criteria, yavision.API_KEY, kimi.API_KEY) = orig

def pages_by_model(model):
    import copy
    return [copy.deepcopy(HW_PAGE if model == "handwritten" else PG_PAGE)]

evs, st, fk = run_pipeline(pages_by_model, lambda m, im: {"r": [{"i": 1, "pick": "B", "read": "праздник", "sure": True}]})
res = [v for k, v in evs if k == "done"][0]
assert st["models"] == ["handwritten", "page"], st      # второй прогон строго ПОСЛЕ основного (не делит с ним квоту Яндекса)
assert ("status", "Перепроверяю неразборчивые слова") in evs
assert res["text"] == "Я считаю что книга праздник важна.", res["text"]
assert len(fk.calls) == 1
ok("run(): два прогона Яндекса (handwritten+page) → спорное слово → Кими выбрала B → в «печатной версии» исправленное слово")

evs, st, fk = run_pipeline(pages_by_model, lambda m, im: {"r": [{"i": 1, "pick": "A", "sure": True}]})
assert [v for k, v in evs if k == "done"][0]["text"] == TXT
ok("run(): Кими подтвердила прочтение handwritten → текст как был (настоящая ошибка ученика сохранена)")

evs, st, fk = run_pipeline(pages_by_model, lambda m, im: RuntimeError("kimi down"))
assert [v for k, v in evs if k == "done"][0]["text"] == TXT and evs[-1][0] == "done"
ok("run(): сбой Кими при перепроверке не ломает проверку — текст Яндекса, итог получен")

def page_fail(model):
    if model == "page": raise yavision.VisionError("net")
    return pages_by_model(model)
evs, st, fk = run_pipeline(page_fail, lambda m, im: {"r": []})
assert [v for k, v in evs if k == "done"][0]["text"] == TXT and not fk.calls
ok("run(): второй прогон Яндекса упал → перепроверка пропущена, проверка идёт как раньше")

import time
async def hang(imgs, model="handwritten", **kw):
    if model == "page":
        await asyncio.sleep(3600)
    return pages_by_model(model)
os.environ["EC_VERIFY_OCR2_TIMEOUT"] = "5"
t0 = time.monotonic()
evs, st, fk = run_pipeline(None, lambda m, im: {"r": []}, recognize=hang)
dt = time.monotonic() - t0
os.environ.pop("EC_VERIFY_OCR2_TIMEOUT")
assert [v for k, v in evs if k == "done"][0]["text"] == TXT and not fk.calls and dt < 15, dt
ok("run(): зависший второй прогон Яндекса держит проверку не дольше EC_VERIFY_OCR2_TIMEOUT (%.0f с здесь)" % dt)

evs, st, fk = run_pipeline(pages_by_model, lambda m, im: {"r": []}, keys=False)
assert st["models"] == [] or st["models"] == ["handwritten"], st
assert not fk.calls and ("status", "Перепроверяю неразборчивые слова") not in evs
ok("run(): без ключей/при выключателе второй прогон и Кими-перепроверка не запускаются")

# --- скорость: второй прогон идёт ПАРАЛЛЕЛЬНО основному (время OCR не складывается), с ограничениями ---
seen = []
async def slow(imgs, model="handwritten", **kw):
    seen.append((model, kw))
    await asyncio.sleep(0.6)
    return pages_by_model(model)
t0 = time.monotonic()
evs, st, fk = run_pipeline(None, lambda m, im: {"r": [{"i": 1, "pick": "B", "read": "праздник", "sure": True}]}, recognize=slow)
dt = time.monotonic() - t0
assert [v for k, v in evs if k == "done"][0]["text"] == "Я считаю что книга праздник важна."
assert dt < 1.0, "два прогона по 0,6 с должны идти параллельно, а не друг за другом: %.2f с" % dt
assert [m for m, _ in seen] == ["handwritten", "page"]                       # основной стартует первым
assert seen[1][1] == {"parallel": ocrverify.PAR2, "retries": 0} and seen[0][1] == {}
ok("run(): второй прогон идёт параллельно основному (2×0,6 с → %.2f с), с parallel=%d и без повторов; основной вызывается как раньше" % (dt, ocrverify.PAR2))

# --- скорость: второй прогон заметно медленнее основного → проверку не задерживаем (grace) ---
async def lag(imgs, model="handwritten", **kw):
    await asyncio.sleep(0.05 if model == "handwritten" else 3.0)
    return pages_by_model(model)
os.environ["EC_VERIFY_GRACE"] = "0.8"
t0 = time.monotonic()
evs, st, fk = run_pipeline(None, lambda m, im: {"r": [{"i": 1, "pick": "B", "read": "праздник", "sure": True}]}, recognize=lag)
dt = time.monotonic() - t0
os.environ.pop("EC_VERIFY_GRACE")
assert [v for k, v in evs if k == "done"][0]["text"] == TXT and not fk.calls and dt < 2.0, dt
ok("run(): второй прогон опаздывает (3 с против 0,05 с) → ждём только grace (%.1f с итого), текст как у Яндекса" % dt)

# --- yavision: retries=0 — одна попытка (второй прогон не жжёт квоту повторами), по умолчанию — как раньше ---
class _Resp:
    status = 503
    async def text(self): return "busy"
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
class _Sess:
    def __init__(self): self.calls = 0
    def post(self, *a, **k): self.calls += 1; return _Resp()
async def _nosleep(*a, **k): return None
orig_sleep, orig_ret = yavision.asyncio.sleep, yavision.RETRIES
yavision.asyncio.sleep = _nosleep; yavision.RETRIES = 2
async def calls(**kw):
    s = _Sess()
    try:
        await yavision._recognize_one(s, asyncio.Semaphore(1), "x", "page", **kw)
    except yavision.VisionError:
        pass
    return s.calls
try:
    c0, c_def = asyncio.run(calls(retries=0)), asyncio.run(calls())
finally:
    yavision.asyncio.sleep, yavision.RETRIES = orig_sleep, orig_ret
assert (c0, c_def) == (1, 3), (c0, c_def)
ok("yavision: retries=0 → одна попытка; без параметра — прежние повторы (RETRIES+1)")

print("test_ocrverify: все проверки прошли")
