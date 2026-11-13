"""«ИИ-проверка» в два этапа: 1) распознавание → текст на проверку учеником, 2) «Текст проверен» → ошибки и критерии ФИПИ.
essaycheck.recognize / run(ocr=…) / apply_text; правки ученика садятся на слова OCR (синхронно с разметкой на фото).
Сеть не вызывается. Запуск: python3 tests/test_essay_two_stage.py"""
import asyncio, base64, copy, io, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", LOG_LEVEL="CRITICAL")
sys.path.insert(0, ROOT)
import essaycheck, kimi, yavision
from PIL import Image

essaycheck.MIN_WORDS = {"ege": 0, "oge": 0}
def ok(c): print("  ✓", c)

def img():
    b = io.BytesIO(); Image.new("RGB", (800, 500), (250, 250, 250)).save(b, "JPEG"); return base64.b64encode(b.getvalue()).decode()

def page(text, specs, y1=10, y2=20):
    """Страница как от Yandex: lw — слова, words — строка целиком и слова (строка первой, как в parse())."""
    lw, cur = [], 0
    for t, x1, x2 in specs:
        p = text.find(t, cur); cur = p + len(t)
        lw.append({"t": t, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "li": 0, "s": p, "n": len(t)})
    line = {"t": " ".join(w["t"] for w in lw), "x1": lw[0]["x1"], "y1": y1, "x2": lw[-1]["x2"], "y2": y2}
    return {"text": text, "words": [line] + [{k: w[k] for k in ("t", "x1", "y1", "x2", "y2")} for w in lw], "lw": lw}

SP = [("Я", 5, 8), ("люблю", 10, 25), ("празник", 27, 45), ("и", 47, 50), ("книги.", 52, 70)]
T0 = "Я люблю празник и книги."
def ocr_of(pg=None, essay=T0):
    return {"pages": [pg or page(T0, SP)], "tpages": [], "essay": essay, "task": "", "extra": "", "part": False, "t_ocr": 0.1, "kind": "ege"}
def toks(o, pi=0): return [w["t"] for w in o["pages"][pi]["lw"]]
def bind(o, found):
    a = {"errors": [{"found": found}]}; essaycheck._bind_coords(a, o["pages"]); e = a["errors"][0]
    return (e.get("page"), e.get("x"), e.get("y")) if "x" in e else None

# ---------- 1. apply_text ----------
o = ocr_of(); before = copy.deepcopy(o)
n, synced = essaycheck.apply_text(o, "Я люблю праздник и книги.")
assert synced and n["essay"] == "Я люблю праздник и книги." and toks(n) == ["Я", "люблю", "праздник", "и", "книги."], toks(n)
assert o == before, "исходный ocr не мутируется"
assert any(w["t"] == "Я люблю праздник и книги." for w in n["pages"][0]["words"]), "строка в words тоже обновлена"
assert [w["t"] for w in n["pages"][0]["words"][1:]][2] == "праздник"
ok("слово заменено: текст, слова OCR (lw) и «words» страницы обновлены, исходные данные не тронуты")

truth = ocr_of(page("Я люблю праздник и книги.", SP), "Я люблю праздник и книги.")       # как если бы OCR прочитал верно
assert bind(n, "праздник") == bind(truth, "праздник") and bind(n, "праздник") is not None
assert all("празник" not in w["t"] for w in n["pages"][0]["words"] + n["pages"][0]["lw"]), "старого слова в разметке нет"
ok("синхронизация с фото: ошибка на исправленном слове садится на ТЕ ЖЕ координаты, что и при верном распознавании; старого слова в разметке нет")

# фрагмент из нескольких слов → другое число слов
SP2 = [("Он", 5, 10), ("любит", 12, 24), ("чтать", 26, 40), ("книгу", 42, 56)]
o2 = ocr_of(page("Он любит чтать книгу", SP2), "Он любит чтать книгу")
n2, s2 = essaycheck.apply_text(o2, "Он любит читать книгу")
assert s2 and toks(n2) == ["Он", "любит", "читать", "книгу"]
n3, s3 = essaycheck.apply_text(o2, "Он очень любит читать книги")                      # 2 слова → 3, плюс вставка и замена
assert s3 and n3["essay"] == "Он очень любит читать книги", n3["essay"]
assert " ".join(toks(n3)) == "Он очень любит читать книги", toks(n3)
n4, s4 = essaycheck.apply_text(o2, "Он любит читать и перечитывать книгу")             # фрагмент длиннее покрытых слов
assert s4 and " ".join(toks(n4)) == "Он любит читать и перечитывать книгу", toks(n4)
assert len([w for w in n4["pages"][0]["lw"]]) == 4, "число слов OCR не меняется — боксы те же"
ok("фрагмент: больше слов, чем покрыто, — лишний текст ложится на последнее слово; вставка — к предыдущему; боксы не плодятся")
n5, s5 = essaycheck.apply_text(o2, "Он любит книгу")                                    # удаление слова
assert s5 and [t for t in toks(n5) if t] == ["Он", "любит", "книгу"] and toks(n5)[2] == "", toks(n5)
assert bind(n5, "любит книгу") is not None
ok("удаление: слово пустеет, соседние слова остаются привязаны к своим боксам")
n6, s6 = essaycheck.apply_text(o2, "Он любит чтать, книгу!")
assert s6 and n6["essay"] == "Он любит чтать, книгу!" and toks(n6) == toks(o2), "правка только пунктуации слова OCR не меняет"
ok("правка одной пунктуации — текст новый, слова OCR прежние")

# перенос слова между строками: два слова OCR ↔ одно слово текста
SP3 = [("Автор", 5, 15), ("пере-", 17, 27), ("несённых", 5, 24)]
o3 = ocr_of(page("Автор пере- несённых", SP3), "Автор перенесённых")
assert "".join(essaycheck._letters(w["t"]) for w in o3["pages"][0]["lw"]) == essaycheck._letters(o3["essay"])
n7, s7 = essaycheck.apply_text(o3, "Автор пережитых")
assert s7 and toks(n7) == ["Автор", "пережитых", ""], toks(n7)
ok("слово, перенесённое на следующую строку (два слова OCR), заменяется целиком")

# несколько правок за раз и повтор
o8 = ocr_of(page("Я думаю что празник важен", [("Я", 5, 8), ("думаю", 10, 25), ("что", 27, 35), ("празник", 37, 55), ("важен", 57, 70)]),
            "Я думаю что празник важен")
n8, s8 = essaycheck.apply_text(o8, "Я думаю, что праздник очень важен")
assert s8 and n8["essay"] == "Я думаю, что праздник очень важен", n8["essay"]
assert toks(n8)[3] == "праздник очень" and toks(n8)[4] == "важен", toks(n8)      # вставка — подсловом после предыдущего слова
n9, s9 = essaycheck.apply_text(n8, "Я думаю, что праздник важен")                      # вторая правка поверх первой
assert s9 and toks(n9)[3] == "праздник" and toks(n9)[4] == "важен", toks(n9)
ok("несколько правок разом и правка поверх правки (включая откат вставки) остаются согласованными")
assert essaycheck.apply_text(o, o["essay"])[0]["pages"] == o["pages"]
ok("текст не менялся — слова OCR те же")

# потоки букв разошлись → текст принимается, слова не трогаются
bad = ocr_of(page(T0, SP), "Совсем другой текст")
nb, sb = essaycheck.apply_text(bad, "Совсем другой текст!")
assert sb is False and nb["essay"] == "Совсем другой текст!" and nb["pages"] == bad["pages"]
nb2, sb2 = essaycheck.apply_text({"pages": [], "essay": "а б"}, "а в")
assert sb2 is False or nb2["essay"] == "а в"
ok("выровнять не удалось → текст принят, слова OCR не тронуты, synced=False (без падения)")
assert essaycheck.apply_text(o, "x" * 50000)[0]["essay"] == "x" * essaycheck.APPLY_MAX
ok("слишком длинный текст обрезается до APPLY_MAX")

# ---------- 2. recognize / run(ocr=…) ----------
CALLS = {"recognize": 0, "msgs": []}
HW = {"text": T0, "words": page(T0, SP)["words"], "lw": page(T0, SP)["lw"]}
async def fake_recognize(imgs, model="handwritten", **kw):
    CALLS["recognize"] += 1
    return [copy.deepcopy(HW) for _ in imgs]
async def fake_stream(messages, **kw):
    CALLS["msgs"].append(messages[-1]["content"])
    yield "think", "думаю"
    yield "text", ('{"errors":[{"type":"Орфография","found":"праздник","correct":"праздник","why":"НН","src":""}],'
                   '"criteria":[{"name":"СК1","got":1,"max":1,"why":"ок","tip":""}],"recommendations":[]}')
async def fake_criteria(kind, fallback=""): return fallback
def with_fakes(fn):
    orig = (yavision.recognize_many, kimi.ask_stream, essaycheck.fipicache.criteria, yavision.API_KEY, kimi.API_KEY)
    yavision.recognize_many, kimi.ask_stream, essaycheck.fipicache.criteria = fake_recognize, fake_stream, fake_criteria
    yavision.API_KEY = kimi.API_KEY = ""                                  # перепроверка слов (ocrverify) выключена
    CALLS.update(recognize=0, msgs=[])
    try:
        return asyncio.run(fn())
    finally:
        (yavision.recognize_many, kimi.ask_stream, essaycheck.fipicache.criteria, yavision.API_KEY, kimi.API_KEY) = orig

async def stage1():
    return [ev async for ev in essaycheck.recognize([img()], [], "ege")]
evs = with_fakes(stage1)
kinds = [k for k, _ in evs]
assert kinds[-1] == "ocr" and set(kinds[:-1]) == {"status"} and "done" not in kinds, kinds
data = evs[-1][1]
assert data["essay"] == T0 and data["kind"] == "ege" and data["pages"][0]["lw"] and data["task"] == "" and data["part"] is False
assert CALLS["recognize"] == 1 and CALLS["msgs"] == [], "этап 1 — только распознавание, Кими не вызывается"
assert evs[0] == ("status", "Собрала 1 фото в одно сочинение") and ("status", "Распознан рукописный текст") in evs
ok("recognize(): только OCR — статусы и данные для этапа 2 (текст, слова, задание), к Кими не обращается")

async def stage2(text):
    ocr, _ = essaycheck.apply_text(data, text)
    return [ev async for ev in essaycheck.run([img()], [], "ege", ocr=ocr)]
evs2 = with_fakes(lambda: stage2("Я люблю праздник и книги."))
assert [k for k, _ in evs2][0] == "status" and evs2[0][1].startswith("Проверяю орфографию") and CALLS["recognize"] == 0
res = [v for k, v in evs2 if k == "done"][0]
assert res["text"] == "Я люблю праздник и книги." and all("празник" not in m for m in CALLS["msgs"]) and any("праздник" in m for m in CALLS["msgs"])
assert res["errors"] and res["errors"][0].get("x") is not None, "ошибка привязана к фото по исправленному слову"
ok("run(ocr=…): OCR не вызывается, Кими анализирует ПОПРАВЛЕННЫЙ текст, ошибка привязана к боксам исправленного слова")

async def onestage():
    return [ev async for ev in essaycheck.run([img()], [], "ege")]
evs1 = with_fakes(onestage)
r1 = [v for k, v in evs1 if k == "done"][0]
evsB = with_fakes(lambda: stage2(T0))
rB = [v for k, v in evsB if k == "done"][0]
assert r1["text"] == rB["text"] and r1["errors"] == rB["errors"] and r1["criteria"] == rB["criteria"]
ok("без правок два этапа дают тот же разбор, что и прежний сквозной run()")

# сбои этапа 1: нет текста / OCR упал — отдаётся итог-сбой, этапа 2 не будет
async def empty(imgs, model="handwritten", **kw): return [{"text": "", "words": [], "lw": []}]
async def boom(imgs, model="handwritten", **kw): raise yavision.VisionError("net")
for rec, why in ((empty, "empty"), (boom, "net")):
    orig = yavision.recognize_many; yavision.recognize_many = rec
    try:
        evs = asyncio.run(stage1())
    finally:
        yavision.recognize_many = orig
    assert evs[-1][0] == "done" and evs[-1][1].get("why") == why and not any(k == "ocr" for k, _ in evs), (why, evs[-1])
ok("этап 1: пустой текст / сбой OCR → done(why), без данных для этапа 2")
print("test_essay_two_stage: все проверки прошли")
