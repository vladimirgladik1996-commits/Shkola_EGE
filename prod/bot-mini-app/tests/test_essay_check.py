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

# --- 5б. экран обработки подключён к стриму проверки (пуш 6), заглушка убрана ---
assert 'apiStream("/api/essay_check/run"' in html, "экран обработки не зовёт /api/essay_check/run"
for s in ('ev.t=="status"', 'ev.t=="done"', 'ev.t=="fail"', 'ev.t=="off"',
          "function ecRetry(", "Повторить", 'id:ecSess,kind:ecKind'):
    assert s in html, f"нет обработки стрима run: {s}"
assert "Распознавание и проверка заработают в следующем обновлении" not in html, \
    "заглушка «заработают в следующем обновлении» должна быть убрана"
ok("экран обработки: живой стрим run (status → done/fail/off), «Повторить», заглушка убрана")

# --- 5в. экран результата (экран 4): текст с ошибками, модалка, вкладки, Т/П, статистика, балл ---
for s in ("function ecResult(", "function ecErrShow(", "function ecErrClose(", "function ecTabSet(",
          "function ecFixSet(", "function ecNrm(", "Фрагмент текста", "Теоретическое обоснование",
          "Источники теории", "Статистика ошибок", "Критерии ФИПИ с обоснованием", "Рекомендации",
          "потенциал +", "Ошибки · ${", "Итоговый балл", "Новая проверка"):
    assert s in html, f"нет строки экрана результата: {s}"
assert '.ec-mod{' in html and '.ec-er{' in html and '.ec-desk{' in html, "нет стилей экрана результата ec-"
assert 'if(e.key==="Escape")ecErrClose()' in html, "модалка ошибки не закрывается по Esc"
assert 'onclick="ecErrShow(' in html, "фрагменты ошибок в тексте не кликабельны"
ok("экран результата: кликабельные ошибки, модалка (фрагмент → ошибка → обоснование → источники), вкладки, Т/П, балл")

# --- 5г. экран результата не падает на пустом результате и повторных тапах ---
for s in ("function ecPot(", "ecRes||ecNrm(null)", 'if(!e)return;', "ec-er\" role=\"button\""):
    assert s in html, f"нет защиты экрана результата: {s}"
assert re.search(r"r\.errors\.filter\(x=>x&&x\.i==i\)\[0\];if\(!e\)return", html), \
    "повторный тап по фрагменту/мусорный номер ошибки должен быть безопасен"
ok("защита результата: пустой результат нормализуется (ecNrm), повторные тапы не палят экран")

# --- 6. API приёма фото: маршрут и поведение ---
import asyncio, base64, hashlib, hmac, io, json, sqlite3, time
from urllib.parse import urlencode
os.environ.update(BOT_TOKEN="***", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import auth, bot
from aiohttp.test_utils import TestClient, TestServer

routes = {}
for res in bot.make_app().router.resources():
    for r in res:
        if res.canonical in ("/api/essay_check/upload", "/api/essay_check/run"):
            routes.setdefault(res.canonical, set()).add(r.method)
assert routes == {"/api/essay_check/upload": {"POST"}, "/api/essay_check/run": {"POST"}}, routes
ok("маршруты POST /api/essay_check/upload и /api/essay_check/run зарегистрированы")

mem = sqlite3.connect(":memory:", check_same_thread=False); bot.db.backup(mem); mem.row_factory = sqlite3.Row
bot.db = mem; auth.init(mem); auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_pins values('other_n', 77)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp()))
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(77,?,?)", (time.time() + 99999, auth._fp())); mem.commit()

def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", bot.TOKEN.encode(), hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)
H = {"X-Init": init(42, "Gladik_N")}
H2 = {"X-Init": init(77, "Other_N")}

from PIL import Image
_buf = io.BytesIO(); Image.new("RGB", (4, 4), "#fff").save(_buf, format="JPEG")
JPG = base64.b64encode(_buf.getvalue()).decode()

# --- 7. пайплайн essaycheck: стадии, нормализация, сбои (kimi замокан, как в test_chat) ---
import essaycheck, kimi
real_ask_json, real_key = kimi.ask_json, kimi.API_KEY

async def fake_ocr_ok(msgs, **kw):
    if kw.get("images"):
        return {"essay": "Автор поднимает проблему влеяния книги.", "task": "Условие: по тексту…"}
    return {"errors": [
        {"type": "Орфография", "found": "влеяния", "correct": "влияния", "why": "проверка гласной", "src": "Розенталь, §…"},
        {"type": "Неведомый тип", "found": "считаю что", "correct": "считаю, что", "why": "запятая перед «что»", "src": "…"},
        {"type": "Пунктуация", "found": ""},                       # без фрагмента — мимо
    ], "criteria": [
        {"name": "К1", "got": 1, "max": 1, "why": "ок", "tip": ""},
        {"name": "К2", "got": 9, "max": 6, "why": "перебор", "tip": "совет"},   # зажмётся до 6
    ], "recommendations": [{"title": "r1", "text": "t1", "badge": "+1"}]}

async def fake_none(msgs, **kw):
    return None

async def fake_empty(msgs, **kw):
    return {"essay": "  "} if kw.get("images") else {}

async def pipeline():
    kimi.API_KEY = "test"; kimi.ask_json = fake_ocr_ok
    evs = [e async for e in essaycheck.run(["img1"], ["img2"], "ege")]
    assert evs[0] == ("status", "Собрала 2 фото в одно сочинение"), evs[0]
    kinds = [k for k, _ in evs]
    assert kinds.count("status") == 5 and kinds[-1] == "done", kinds
    res = dict(evs)["done"]
    assert res["text"] == "Автор поднимает проблему влеяния книги." and res["task"] == "Условие: по тексту…"
    assert [e["i"] for e in res["errors"]] == [1, 2]               # мимо-запись отсеяна, номера подряд
    assert res["errors"][1]["type"] == "Речь"                      # неизвестный тип → «Речь»
    assert res["criteria"][1]["got"] == 6                          # зажато в максимум критерия
    assert res["score"] == {"got": 7, "max": 7, "percent": 100, "level": "Отлично"}
    ok("пайплайн: 5 стадий, разбор нормализуется (типы, нумерация, баллы, итог)")

    kimi.ask_json = fake_none
    assert [e async for e in essaycheck.run(["x"], [], "oge")][-1] == ("err", "net")
    kimi.ask_json = fake_empty
    assert [e async for e in essaycheck.run(["x"], [], "ege")][-1] == ("err", "empty")
    ok("сбои этапов: модель молчит → err/net, пустое распознавание → err/empty")

asyncio.run(pipeline())

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

        # --- 8. /api/essay_check/run: стрим стадий, результат, отказы ---
        assert (await c.post("/api/essay_check/run", json={"id": "нет-такого"}, headers=H)).status == 400
        assert (await c.post("/api/essay_check/run", json={"id": d["id"]}, headers=H2)).status == 403
        ok("run: неизвестный сеанс → 400, чужой сеанс → 403")

        kimi.API_KEY = ""
        r = await c.post("/api/essay_check/run", json={"id": d["id"]}, headers=H)
        assert [json.loads(x) for x in (await r.text()).splitlines() if x.strip()] == [{"t": "off"}]
        kimi.API_KEY = "test"
        ok("run без подключённого ИИ → событие off")

        kimi.ask_json = fake_ocr_ok
        r = await c.post("/api/essay_check/run", json={"id": d["id"], "kind": "ege"}, headers=H)
        evs = [json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
        assert r.status == 200 and [e["t"] for e in evs].count("status") == 5 and evs[-1]["t"] == "done", evs
        res = evs[-1]["result"]
        assert res["score"]["max"] == 7 and res["errors"][0]["found"] == "влеяния"
        assert {"text", "task", "errors", "criteria", "recommendations", "score"} <= set(res), sorted(res)
        assert set(res["score"]) == {"got", "max", "percent", "level"} and res["score"]["level"] == "Отлично"
        assert set(res["errors"][0]) == {"i", "type", "found", "correct", "why", "src"}, sorted(res["errors"][0])
        assert set(res["criteria"][0]) == {"name", "got", "max", "why", "tip"}
        assert set(res["recommendations"][0]) == {"title", "text", "badge"}
        ok("run: 5 стадий и нормализованный разбор в done.result (схема экрана результата)")

        kimi.ask_json = fake_none
        r = await c.post("/api/essay_check/run", json={"id": d["id"]}, headers=H)
        evs = [json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
        assert evs[-1] == {"t": "fail", "why": "net"}, evs

        kimi.ask_json = fake_empty                       # пустой разбор — тоже честный сбой, а не падение фронта
        r = await c.post("/api/essay_check/run", json={"id": d["id"]}, headers=H)
        evs = [json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
        assert evs[-1] == {"t": "fail", "why": "empty"}, evs
        ok("пустое распознавание/разбор → fail/empty")

        kimi.ask_json = fake_ocr_ok                      # «Повторить» после сбоя: тот же сеанс проверяется заново
        r = await c.post("/api/essay_check/run", json={"id": d["id"]}, headers=H)
        evs = [json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
        assert evs[-1]["t"] == "done" and evs[-1]["result"]["text"], evs[-1]
        ok("повторный run по тому же сеансу работает (кнопка «Повторить» на экране сбоя)")

        kimi.ask_json = real_ask_json; kimi.API_KEY = real_key
        ok("сбой модели → fail/net (фронт увидит честный отказ)")

asyncio.run(main())
print("test_essay_check: OK")
