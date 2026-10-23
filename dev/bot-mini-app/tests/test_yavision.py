"""yavision.py: разбор ответа Yandex Vision OCR (handwritten) и привод координат.
Сеть не вызывается — только parse() и фикстуры в формате настоящего API."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("YANDEX_API_KEY", None)
import yavision
def ok(c): print("  ✓", c)

def verts(x1, y1, x2, y2):
    return {"vertices": [{"x": str(x1), "y": str(y1)}, {"x": str(x1), "y": str(y2)},
                         {"x": str(x2), "y": str(y2)}, {"x": str(x2), "y": str(y1)}]}

# ---------- 1. полный ответ: текст + слова с боксами в процентах ----------
fixture = {"result": {"textAnnotation": {
    "width": "900", "height": "300", "fullText": "Привет мир", "rotate": "ANGLE_0",
    "blocks": [{"boundingBox": verts(10, 10, 200, 30), "lines": [
        {"boundingBox": verts(10, 10, 200, 30), "words": [
            {"text": "Привет", "boundingBox": verts(10, 10, 100, 30)},
            {"text": "мир", "boundingBox": verts(110, 10, 200, 30)}]}]}]}}}
r = yavision.parse(fixture)
assert r["text"] == "Привет мир" and len(r["words"]) == 2
w0, w1 = r["words"]
assert (w0["t"], w1["t"]) == ("Привет", "мир")
assert abs(w0["x1"] - 10 * 100 / 900) < 0.2 and abs(w0["x2"] - 100 * 100 / 900) < 0.2
assert abs(w0["y1"] - 10 * 100 / 300) < 0.2 and abs(w0["y2"] - 30 * 100 / 300) < 0.2
assert 0 <= w0["x1"] < w0["x2"] <= 100 and 0 <= w0["y1"] < w0["y2"] <= 100
ok("parse(): fullText + слова с координатами в процентах (пиксели → % по width/height)")

# ---------- 2. толерантность: пустой ответ, слова без боксов, мусор ----------
assert yavision.parse({}) == {"text": "", "words": []}
assert yavision.parse({"result": {}}) == {"text": "", "words": []}
assert yavision.parse({"result": {"textAnnotation": {"fullText": "  только текст  "}}})["text"] == "только текст"
no_box = {"result": {"textAnnotation": {"width": "10", "height": "10", "fullText": "а",
          "blocks": [{"lines": [{"words": [{"text": "а"}]}]}]}}}
assert yavision.parse(no_box)["words"] == []           # слово без бокса не попадает в разметку
bad_box = {"result": {"textAnnotation": {"width": "10", "height": "10", "fullText": "а",
           "blocks": [{"lines": [{"words": [{"text": "а", "boundingBox": {"vertices": "мусор"}}]}]}]}}}
assert yavision.parse(bad_box)["words"] == []
ok("parse(): пустой ответ, слова без координат и мусорные боксы не ломают разбор")

# ---------- 3. координаты за краями фото зажимаются, отрицательные отбрасываются ----------
over = {"result": {"textAnnotation": {"width": "100", "height": "100", "fullText": "x",
        "blocks": [{"lines": [{"words": [
            {"text": "внутри", "boundingBox": verts(-20, -5, 120, 105)},
            {"text": "снаружи", "boundingBox": verts(500, 500, 600, 600)}]}]}]}}}
ws = yavision.parse(over)["words"]
assert len(ws) == 1 and ws[0]["t"] == "внутри" and ws[0]["x1"] == 0 and ws[0]["y1"] == 0
assert ws[0]["x2"] == 100 and ws[0]["y2"] == 100
ok("parse(): выход за края зажимается в 0–100, полностью внешний бокс отбрасывается")

# ---------- 4. размер без width/height: значения трактуются как доли (0–1) ----------
frac = {"result": {"textAnnotation": {"fullText": "x", "blocks": [{"lines": [{"words": [
        {"text": "a", "boundingBox": verts(0.1, 0.2, 0.5, 0.4)}]}]}]}}}
wf = yavision.parse(frac)["words"][0]
assert abs(wf["x1"] - 10) < 0.2 and abs(wf["y1"] - 20) < 0.2 and abs(wf["x2"] - 50) < 0.2
ok("parse(): без width/height координаты считаются долями фото и переводятся в проценты")

# ---------- 5. enabled() зависит только от ключа в env ----------
assert yavision.enabled() is False
os.environ["YANDEX_API_KEY"] = "test-key"
import importlib; importlib.reload(yavision)
assert yavision.enabled() is True
os.environ.pop("YANDEX_API_KEY"); importlib.reload(yavision)
assert yavision.enabled() is False
ok("enabled(): ключ только из env, без ключа боевой OCR выключен")

print("test_yavision: все проверки прошли")
