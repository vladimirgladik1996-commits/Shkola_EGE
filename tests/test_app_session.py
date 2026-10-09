"""Standalone-вход (APK вне Telegram): одноразовая ссылка + пароль → API-токен, запросы с X-Session."""
import asyncio, hashlib, hmac, json, os, sqlite3, sys, time
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
mem.commit()
def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)
H = {"X-Init": init(42, "Gladik_N")}

async def main():
    app = bot.make_app()
    auth.setup(app)                                  # как в bot.run(): регистрирует /login и /api/login
    async with TestClient(TestServer(app)) as c:
        # --- выдача токена через обычный вход (ссылка из бота + логин/пароль) ---
        t = auth.new_link(42, "Gladik_N").split("#")[1]
        r = await c.post("/api/login", json={"t": t, "login": "Admin", "password": "Tst-Only-Pw-7qZ4!"},
                         headers={"Origin": auth.BASE, "Sec-Fetch-Site": "same-origin"})
        j = await r.json()
        assert r.status == 200 and j["ok"] and j.get("token"), (r.status, j)
        tok = j["token"]
        ok("вход по ссылке+паролю выдаёт API-токен")

        # --- X-Session работает как X-Init: тот же API, тот же учёт ---
        r = await c.get("/api/data", headers={"X-Session": tok})
        assert r.status == 200, r.status
        r2 = await c.post("/api/student/add", json={"name": "Тест", "price": 1000}, headers={"X-Session": tok})
        assert r2.status == 200, r2.status
        ok("X-Session: /api/data и запись изменений работают, журнал ведётся от tg_id владельца токена")
        rows = [tuple(x) for x in mem.execute("select tg_id, action from audit_log where action='/api/student/add'")]
        assert rows and rows[-1][0] == 42, rows

        # --- мусор вместо токена → 403, без исключений ---
        for bad in ("", "short", "x" * 300, "'; drop table api_tokens; --", "AAAA", "a" * 43 + "="):
            r = await c.get("/api/data", headers={"X-Session": bad})
            assert r.status == 403, (bad[:20], r.status)
        r = await c.get("/api/data")
        assert r.status == 403
        ok("битый/чужой/пустой токен и запрос без него → 403")

        # --- скользящее продление: срок растёт при использовании ---
        mem.execute("update api_tokens set exp=? where tg_id=?", (time.time() + 120, 42)); mem.commit()
        r = await c.get("/api/data", headers={"X-Session": tok}); assert r.status == 200, (r.status, await r.text())
        exp = mem.execute("select exp from api_tokens where tg_id=?", (42,)).fetchone()[0]
        assert exp > time.time() + 121, exp
        ok("срок токена продлевается при использовании (скользящая сессия)")

        # --- истёкший токен гасится ---
        mem.execute("update api_tokens set exp=? where tg_id=?", (time.time() - 1, 42)); mem.commit()
        r = await c.get("/api/data", headers={"X-Session": tok}); assert r.status == 403
        assert mem.execute("select count(*) from api_tokens where tg_id=?", (42,)).fetchone()[0] == 0
        ok("просроченный токен → 403 и удаляется")

        # --- исключение из белого списка закрывает токен немедленно ---
        tok = auth.issue_token(42)
        mem.execute("delete from auth_pins where tg_id=?", (42,)); mem.commit()
        r = await c.get("/api/data", headers={"X-Session": tok}); assert r.status == 403
        mem.execute("insert into auth_pins values('gladik_n', 42)"); mem.commit()
        ok("исключение из белого списка мгновенно гасит токен")

        # --- /logout в боте отзывает и токены приложения ---
        tok = auth.issue_token(42)
        auth.logout(42)
        r = await c.get("/api/data", headers={"X-Session": tok}); assert r.status == 403
        ok("auth.logout() отзывает и API-токены")

        # --- Telegram-путь не задет: X-Init работает как раньше ---
        mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp())); mem.commit()
        r = await c.get("/api/data", headers=H); assert r.status == 200, (r.status, await r.text())
        ok("обычный вход через Telegram (X-Init) работает как раньше")

        # --- пара токенов: старый гасится при повторном входе? нет — токены накапливаются, но logout чистит все ---
        auth.issue_token(42); auth.issue_token(42)
        n = mem.execute("select count(*) from api_tokens where tg_id=?", (42,)).fetchone()[0]
        auth.logout(42)
        assert n >= 2 and mem.execute("select count(*) from api_tokens where tg_id=?", (42,)).fetchone()[0] == 0
        ok("повторные входы дают новые токены; logout чистит все сразу")

asyncio.run(main())
print("OK")
