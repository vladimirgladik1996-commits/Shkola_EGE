"""Контракт раздела «ИИ-проверка»: меню, каркас экранов и приём фото (пуши 3–4 из PUSH-PLAN.md).
Строки фронта проверяются по docs/index.html; API — живым тестом через TestClient.
Запуск: python3 tests/test_essay_check.py"""
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def ok(c): print("  ✓", c)

html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()

# --- 1. пункт меню и роутер ---
assert 'data-t="ai"' in html, "нет пункта меню «ИИ-проверка»"
assert re.search(r'ORDER=\["ch","c","p","s","ai","mt"\]', html), "«ai» выпал из ORDER"
assert 'ai:"ИИ-проверка"' in html, "нет имени раздела в NAMES"
ok("меню и роутер: пункт «ИИ-проверка» (data-t=ai) на месте")

# --- 2. render() ведёт tab=ai на каркас, а не на заглушку «Появится позже» ---
assert 'tab=="ai"?aiCheck()' in html, "render() не зовёт aiCheck для tab=ai"
assert "function aiCheck(" in html and "function ecShot(" in html and "function ecProc(" in html, "нет функций раздела"
ok("render(): tab=ai → aiCheck() — заглушка обойдена")

# --- 3. экран «Старт»: оффер и два выбора ---
for s in ("Сфотографируйте работу", "Сочинение для ЕГЭ", "Эссе для ОГЭ", "по критериям ФИПИ"):
    assert s in html, f"нет строки старта: {s}"
ok("экран «Старт»: оффер и два выбора (ЕГЭ / ОГЭ) на месте")

# --- 4. экран «Съёмка»: кадр, подсказки, наборы «Работа» / «Задание», затвор и галерея ---
for s in ("Положите лист ровно", "Снимите условие задания",
          "Снимайте страницы по порядку", "Работа · ${", "Задание · ${", "Из галереи"):
    assert s in html, f"нет строки съёмки: {s}"
assert "function ecCamOpen(" in html and "function ecUpload(" in html and "function ecInp(" in html
ok("экран «Съёмка»: кадр, наборы с живыми счётчиками, затвор (camTake) и галерея")

# --- 5. экран обработки и стили в своём неймспейсе (урок коллизии .cf из коммита 4d5fa3e) ---
for s in ("Вишенка читает", "Распознан рукописный текст", "Сверяю с критериями ФИПИ", "Новая проверка"):
    assert s in html, f"нет строки обработки: {s}"
assert ".ec-hero{" in html and ".ec-shut{" in html and ".ec-cam{" in html, "нет стилей ec-"
ok("экран обработки и стили ec- на месте")

# --- 6. API приёма фото: маршрут и поведение ---
import asyncio, base64, hashlib, hmac, io, json, sqlite3, time
from urllib.parse import urlencode
os.environ.update(BOT_TOKEN="***", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import auth, bot
from aiohttp.test_utils import TestClient, TestServer

routes = set()
for res in bot.make_app().router.resources():
    for r in res:
        if res.canonical == "/api/essay_check/upload":
            routes.add(r.method)
assert routes == {"POST"}, routes
ok("маршрут POST /api/essay_check/upload зарегистрирован")

mem = sqlite3.connect(":memory:", check_same_thread=False); bot.db.backup(mem); mem.row_factory = sqlite3.Row
bot.db = mem; auth.init(mem); auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp())); mem.commit()

def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", bot.TOKEN.encode(), hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)
H = {"X-Init": init(42, "Gladik_N")}

from PIL import Image
_buf = io.BytesIO(); Image.new("RGB", (4, 4), "#fff").save(_buf, format="JPEG")
JPG = base64.b64encode(_buf.getvalue()).decode()

async def main():
    app = bot.make_app()
    async with TestClient(TestServer(app)) as c:
        r = await c.post("/api/essay_check/upload", json={"w": [{"d": JPG}], "t": []})
        assert r.status == 403, r.status
        ok("загрузка без входа → 403")

        for body in ({"w": [], "t": []}, {"w": 1, "t": []}, {"w": [{"d": "не base64!"}], "t": []},
                     {"w": [{"d": base64.b64encode(b"not an image").decode()}], "t": []}):
            r = await c.post("/api/essay_check/upload", json=body, headers=H)
            assert r.status == 400, (body, r.status)
        ok("пустые наборы, мусор и не-картинки → 400")

        r = await c.post("/api/essay_check/upload",
                         json={"w": [{"n": "1.jpg", "d": JPG, "mime": "image/jpeg"}],
                               "t": [{"n": "2.jpg", "d": JPG, "mime": "image/jpeg"}]}, headers=H)
        d = await r.json()
        assert r.status == 200 and d["n_w"] == 1 and d["n_t"] == 1 and d["id"], (r.status, d)
        assert d["id"] in bot.EC_CHECKS and bot.EC_CHECKS[d["id"]][0] == 42
        ok("фото приняты: сеанс создан (id, счётчики наборов), владелец записан")

        r = await c.post("/api/essay_check/upload",
                         json={"w": [{"d": JPG}] * 13, "t": []}, headers=H)
        assert r.status == 400, r.status
        ok("больше 12 фото на проверку → 400")

asyncio.run(main())
print("test_essay_check: OK")
