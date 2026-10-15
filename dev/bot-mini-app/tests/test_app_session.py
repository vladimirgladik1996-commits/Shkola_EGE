"""Standalone-вход (APK вне Telegram): только пароль — отдельная от Telegram сессия;
плюс устаревший вход по одноразовой ссылке. Запросы с X-Session — тот же API, что и мини-апп."""
import asyncio, hashlib, hmac, json, os, sqlite3, sys, time
from urllib.parse import urlencode
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
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
        # --- выдача токена через обычный вход (ссылка из бота + пароль) ---
        t = auth.new_link(42, "Gladik_N").split("#")[1]
        r = await c.post("/api/login", json={"t": t, "password": "Tst-Only-Pw-7qZ4!"},
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

        # --- standalone-вход (APK): ТОЛЬКО пароль, без ссылки из бота и Telegram ---
        r = await c.post("/api/login", json={"password": "Tst-Only-Pw-7qZ4!"},
                         headers={"Origin": auth.BASE, "Sec-Fetch-Site": "same-origin"})
        j = await r.json()
        assert r.status == 200 and j["ok"] and j.get("token"), (r.status, j)
        stok = j["token"]
        r = await c.get("/api/data", headers={"X-Session": stok})
        assert r.status == 200, r.status
        assert mem.execute("select tg_id from api_tokens where h=?", (auth._h(stok),)).fetchone()[0] == 0
        ok("standalone: вход только по паролю выдаёт токен (tg_id=0), тот же API работает")

        # --- независимость от Telegram: logout Telegram-пользователя не трогает standalone-токены ---
        auth.issue_token(42); auth.logout(42)
        r = await c.get("/api/data", headers={"X-Session": stok}); assert r.status == 200, r.status
        ok("вход/выход в Telegram-мини-аппе не влияет на standalone-сессию и наоборот")

        # --- неверные пароли → 401, после лимита → 429; смена пароля гасит standalone-токены ---
        for i in range(auth.MAX_FAILS):
            r = await c.post("/api/login", json={"password": "wrong-pass"},
                             headers={"Origin": auth.BASE, "Sec-Fetch-Site": "same-origin"})
            assert r.status == 401, r.status
        r = await c.post("/api/login", json={"password": "Tst-Only-Pw-7qZ4!"},
                         headers={"Origin": auth.BASE, "Sec-Fetch-Site": "same-origin"})
        assert r.status == 429, r.status
        mem.execute("delete from auth_fails where tg_id=?", (auth.STANDALONE_UID,)); mem.commit()
        old_hash = auth.PW_HASH; auth.PW_HASH = auth.make_hash("Other-Standalone-Pw-1!")
        r = await c.get("/api/data", headers={"X-Session": stok}); assert r.status == 403
        auth.PW_HASH = old_hash
        ok("standalone: лимит попыток работает, смена пароля гасит выданные токены")

        # --- неполные данные входа → 400 без траты попыток ---
        for b in (b"{}", b'{"password":""}', b'{"t":"","password":""}', b'{"t":"x"}', b'{"login":"Admin"}'):
            rr = await c.post("/api/login", data=b, headers={"Origin": auth.BASE, "Sec-Fetch-Site": "same-origin",
                                                             "Content-Type": "application/json"})
            assert rr.status == 400, (b, rr.status)
        ok("неполные данные входа → 400")

        # --- оплаты и занятия из APK (X-Session): полный цикл без сюрпризов ---
        r = await c.post("/api/login", json={"password": "Tst-Only-Pw-7qZ4!"},
                         headers={"Origin": auth.BASE, "Sec-Fetch-Site": "same-origin"})
        stok = (await r.json())["token"]
        r = await c.post("/api/student/add", json={"name": "Оплата Тест", "price": 1500}, headers={"X-Session": stok})
        sid = (await r.json())["id"]
        r = await c.post("/api/payment/add", json={"student_id": sid, "amount": 3000}, headers={"X-Session": stok})
        j = await r.json()
        assert r.status == 200 and j.get("ok") and j.get("id"), (r.status, j)
        pid = j["id"]
        r = await c.get("/api/data", headers={"X-Session": stok})
        pays = [p for p in (await r.json())["payments"] if p["id"] == pid]
        assert pays and pays[0]["student_id"] == sid and pays[0]["amount"] == 3000, pays
        r = await c.post("/api/payment/update", json={"id": pid, "amount": 4500}, headers={"X-Session": stok})
        assert r.status == 200, r.status
        r = await c.post("/api/payment/delete", json={"id": pid}, headers={"X-Session": stok})
        assert r.status == 200, r.status
        r = await c.post("/api/lesson/add", json={"student_id": sid, "day": "2027-01-12", "time": "15:00", "dur": 60},
                         headers={"X-Session": stok})
        assert r.status == 200, (r.status, await r.text())
        r = await c.get("/api/data", headers={"X-Session": stok})
        ls = [l for l in (await r.json())["lessons"] if l["day"] == "2027-01-12" and l["student_id"] == sid]
        assert ls, "занятие не встало в расписание"
        # несуществующий ученик — честный 404 no_student, а не молчаливая потеря записи
        r = await c.post("/api/payment/add", json={"student_id": 999999, "amount": 100}, headers={"X-Session": stok})
        assert r.status == 404 and (await r.json()).get("error") == "no_student", r.status
        ok("из APK: оплата (добавить/изменить/удалить) и занятие в календарь проходят; чужой id ученика → 404 no_student")

asyncio.run(main())
print("OK")
