"""Голосовой помощник: /api/voice принимает запись и контекст страницы, проверяет их и отвечает заглушкой."""
import asyncio, hashlib, hmac, json, logging, os, sqlite3, sys, time
from urllib.parse import urlencode
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", AUTH_LOGIN="Admin", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
_db_existed = os.path.exists(os.path.join(ROOT, "data", "tutor.db"))
import auth, bot
from aiohttp.test_utils import TestClient, TestServer
def ok(c): print("  ✓", c)

mem = sqlite3.connect(":memory:", check_same_thread=False); bot.db.backup(mem); mem.row_factory = sqlite3.Row
bot.db = mem; auth.init(mem); auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp())); mem.commit()
def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)
H = {"X-Init": init(42, "Gladik_N")}


import base64
AUD = base64.b64encode(b"\x1aE\xdf\xa3fake-webm").decode()
CTX = {"tab": "p", "screen": "payment_new", "screen_args": [5], "student_id": 5, "date_from": "2026-10-01"}

async def main():
    async with TestClient(TestServer(bot.make_app())) as c:
        post = lambda j, h=H: c.post("/api/voice", json=j, headers=h)
        assert (await c.post("/api/voice", json={"ctx": CTX, "audio": AUD, "mime": "audio/webm"})).status == 403
        assert (await post({"ctx": CTX, "audio": AUD, "mime": "audio/webm"}, {"X-Init": init(7, "evil_user")})).status == 403
        ok("без подписи и у постороннего → 403")

        r = await post({"ctx": CTX, "audio": AUD, "mime": "audio/webm", "dur": 3.2}); j = await r.json()
        assert r.status == 200 and j["ok"] and j["context"]["tab"] == "p" and j["context"]["tab_name"] == "Оплаты"
        assert j["context"]["screen"] == "payment_new" and j["context"]["screen_args"] == [5] and j["context"]["student_id"] == 5
        assert "Оплаты" in j["reply"]
        ok("команда с вкладки «Оплаты», экран «новая оплата» → бэкенд знает вкладку, экран, ученика и период")

        r = await post({"ctx": {"tab": "c", "view": "w", "week_start": "2026-10-05", "month": "2026-10-01"}, "audio": AUD, "mime": "audio/webm"})
        j = await r.json(); assert r.status == 200 and j["context"]["screen"] is None and j["context"]["week_start"] == "2026-10-05"
        r = await post({"ctx": {"tab": "c", "screen": "day_card", "screen_args": ["2026-10-12"]}, "text": "перенеси на завтра"})
        j = await r.json(); assert r.status == 200 and j["context"]["screen_args"] == ["2026-10-12"]
        ok("расписание без открытого экрана; карточка дня с датой; можно прислать готовый текст вместо звука")

        r = await post({"ctx": {**CTX, "admin": True, "evil": "x"}, "audio": AUD, "mime": "audio/webm"}); j = await r.json()
        assert r.status == 200 and "admin" not in j["context"] and "evil" not in j["context"]
        ok("лишние ключи контекста отбрасываются")

        bad = [
            {"audio": AUD, "mime": "audio/webm"},                                              # нет контекста
            {"ctx": {"tab": "hack"}, "audio": AUD, "mime": "audio/webm"},                      # неизвестная вкладка
            {"ctx": {**CTX, "screen": "drop_table"}, "audio": AUD, "mime": "audio/webm"},      # неизвестный экран
            {"ctx": {**CTX, "screen_args": ["x; drop"]}, "audio": AUD, "mime": "audio/webm"},  # arg не id и не дата
            {"ctx": {"tab": "c", "view": "zzz"}, "audio": AUD, "mime": "audio/webm"},
            {"ctx": {"tab": "p", "date_from": "20261001"}, "audio": AUD, "mime": "audio/webm"},
            {"ctx": CTX},                                                                      # ни звука, ни текста
            {"ctx": CTX, "audio": "!!!не base64!!!", "mime": "audio/webm"},
            {"ctx": CTX, "audio": AUD, "mime": "text/html"},                                   # не аудио
            {"ctx": CTX, "audio": AUD, "mime": "audio/webm", "dur": 999},
            {"ctx": CTX, "audio": "A" * (bot.VOICE_MAX_B64 + 4), "mime": "audio/webm"},        # слишком большой
            {"ctx": CTX, "text": "x" * 1001},
            [1, 2, 3],
        ]
        for i, b in enumerate(bad):
            r = await post(b); assert r.status == 400, (i, r.status)
        ok(f"{len(bad)} некорректных запросов → 400 (контекст проверяется по белому списку)")

        rows = [tuple(r) for r in mem.execute("select tg_id, action, target from audit_log where action='/api/voice'")]
        assert rows and all(r[0] == 42 and AUD not in r[2] for r in rows)
        ok("команды попадают в журнал действий, звук в журнал не пишется")
asyncio.run(main())
print("OK")
