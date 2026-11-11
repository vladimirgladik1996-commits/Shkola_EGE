"""tools/ocr_eval.py: метрики и обвязка замера (Яндекс/Кими подменены). Запуск: python3 tests/test_ocr_eval.py"""
import asyncio, base64, io, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", LOG_LEVEL="CRITICAL")
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools"))
import ocr_eval, essaycheck, kimi, yavision
from PIL import Image

def ok(c): print("  ✓", c)

# метрики
assert ocr_eval.wer("Я люблю книги", "я люблю книги.") == 0 and ocr_eval.cer("Ёлка", "елка") == 0
assert abs(ocr_eval.wer("а б в г", "а б х г") - 0.25) < 1e-9
assert ocr_eval.tokens("По-моему, кто-то пришёл!") == ["по-моему", "кто-то", "пришел"]
assert ocr_eval.wer("", "") == 0 and ocr_eval.cer("а", "") == 1
ok("WER/CER: регистр, ё=е, пунктуация не считаются; дефисные слова — одно слово")

# пределы фото совпадают с ботом
bot = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
for name, val in zip(("EC_MAX_SIDE", "EC_MAX_PIXELS", "EC_MAX_BYTES"), (ocr_eval.EC_MAX_SIDE, ocr_eval.EC_MAX_PIXELS, ocr_eval.EC_MAX_BYTES)):
    m = re.search(r"^%s = ([0-9_]+)" % name, bot, re.M)
    assert m and int(m.group(1).replace("_", "")) == val, (name, m and m.group(1))
ok("пределы фото в замере совпадают с bot.py")

# обвязка: old ужимает до 2600 и теряет дефис, new отдаёт крупнее и дефис сохраняет
buf = io.BytesIO(); Image.new("RGB", (4000, 3000), (240, 240, 240)).save(buf, "JPEG"); RAW = buf.getvalue()
sizes = []
async def fake_recognize(imgs, model="handwritten"):
    sizes.append((model, Image.open(io.BytesIO(base64.b64decode(imgs[0]))).size))
    return [{"text": "Он сказал по-\nмоему мнению.", "words": [], "lw": []}]
orig = yavision.recognize_many; yavision.recognize_many = fake_recognize
try:
    res = asyncio.run(ocr_eval.evaluate([("p1", RAW, "Он сказал по-моему мнению.")], ["old", "new"]))
finally:
    yavision.recognize_many = orig
(_, w_old, _, t_old, _), (_, w_new, _, t_new, _) = res["old"][0], res["new"][0]
assert max(sizes[0][1]) == 2600 and sizes[1][1] == (4000, 3000), sizes
assert "помоему" in t_old and "по-моему" in t_new and w_old > 0 and w_new == 0, (t_old, t_new)
assert "p1" in ocr_eval.report(res) and "СРЕДНЕЕ" in ocr_eval.report(res)
ok("замер: old — 2600 px и потеря дефиса, new — 4000×3000 и дефис на месте; отчёт строится")

# load(): пары фото+эталон
import tempfile
with tempfile.TemporaryDirectory() as d:
    open(os.path.join(d, "a.jpg"), "wb").write(RAW); open(os.path.join(d, "a.txt"), "w", encoding="utf-8").write("текст")
    open(os.path.join(d, "b.jpg"), "wb").write(RAW)                      # без эталона — пропускается
    assert [i[0] for i in ocr_eval.load(d)] == ["a"]
ok("load(): берёт только пары имя.jpg + имя.txt")
print("test_ocr_eval: все проверки прошли")
