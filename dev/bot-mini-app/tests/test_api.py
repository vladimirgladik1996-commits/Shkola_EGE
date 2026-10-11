"""HTTP-уровень настоящих обработчиков API + миграции схемы + журнал действий + лимит запросов.
Сеть Telegram не используется; база — в памяти."""
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

# ---------- 1. миграции: старая база без новых колонок и таблиц ----------
old = sqlite3.connect(":memory:")
old.executescript("""
create table students(id integer primary key, name text not null, price integer default 0, photo text);
create table lessons(id integer primary key, student_id integer not null, day text not null, time text not null,
  regular_id integer, dur integer default 60, unique(day,time));
create table payments(id integer primary key, student_id integer not null, amount integer not null, created text);
create table regular(id integer primary key, student_id integer not null, weekday integer not null, time text not null, start text not null, dur integer default 60);
create table skips(regular_id integer, day text, primary key(regular_id, day));
insert into students(id,name,price) values(1,'Иван',1500); insert into payments(student_id,amount) values(1,3000);""")
assert bot.init_db(old) is None and old.execute("pragma user_version").fetchone()[0] == len(bot.MIGRATIONS)
cols = {r[1] for r in old.execute("pragma table_info(students)")}
assert {"grade", "subject", "deleted"} <= cols and old.execute("select name from sqlite_master where name='audit_log'").fetchone()
assert old.execute("select name, price from students").fetchone() == ("Иван", 1500) and old.execute("select amount from payments").fetchone()[0] == 3000
bot.init_db(old); assert old.execute("pragma user_version").fetchone()[0] == len(bot.MIGRATIONS)       # повторный запуск безопасен
ok(f"старая база мигрирует до версии {len(bot.MIGRATIONS)} без потери данных; повторный запуск идемпотентен")

# ---------- 2. API ----------
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

async def main():
    async with TestClient(TestServer(bot.make_app())) as c:
        post = lambda p, j, h=H: c.post(p, json=j, headers=h)
        assert (await c.get("/api/data", headers={"X-Init": init(7, "evil_user")})).status == 403
        assert (await c.get("/api/data")).status == 403
        r = await c.get("/api/data", headers=H); assert r.status == 200 and r.headers["Cache-Control"] == "no-store"
        ok("посторонний и запрос без подписи → 403; владелец → 200, ответ не кешируется")

        r = await post("/api/student/add", {"name": "Анна Петрова", "price": 2000, "photo": None}); assert r.status == 200
        sid = mem.execute("select id from students where name='Анна Петрова'").fetchone()[0]
        assert (await post("/api/lesson/add", {"student_id": sid, "day": "2026-10-12", "time": "10:00"})).status == 200
        assert (await post("/api/payment/add", {"student_id": sid, "amount": 2000})).status == 200
        assert (await post("/api/lesson/add", {"student_id": sid, "day": "20261012", "time": "11:00"})).status == 400   # без записи в журнал
        d = await (await c.get("/api/data", headers=H)).json(); assert d
        ok("основной сценарий: ученик → занятие → оплата → данные")

        rows = [tuple(r) for r in mem.execute("select tg_id, action, target from audit_log order by id")]
        assert [r[1] for r in rows] == ["/api/student/add", "/api/lesson/add", "/api/payment/add"], rows
        assert all(r[0] == 42 for r in rows) and "Анна" not in json.dumps(rows, ensure_ascii=False)
        assert "amount=2000" in rows[2][2] and f"student_id={sid}" in rows[1][2] and "day=2026-10-12" in rows[1][2]
        ok("журнал действий: кто (tg_id), что и над чем (id, день, сумма); имён/фото нет; неудачные запросы не пишутся")

        # 500: непредвиденная ошибка → общий JSON без деталей, в лог (не наружу)
        async def boom(request): raise sqlite3.OperationalError("database is locked: secret-detail")
        g = bot.guarded(boom)
        from aiohttp import web
        app3 = web.Application(); app3.router.add_post("/x", g)
        async with TestClient(TestServer(app3)) as c3:
            r = await c3.post("/x", json={}, headers=H); t = await r.text()
            assert r.status == 500 and "secret-detail" not in t and json.loads(t) == {"error": "internal"}, (r.status, t)
        ok("непредвиденная ошибка → 500 с общим телом, деталей наружу нет")

        # лимит запросов
        bot.RATE_LIMIT = 5; bot._hits.clear()
        codes = [(await c.get("/api/data", headers=H)).status for _ in range(8)]
        assert codes[:5] == [200] * 5 and codes[5:] == [429] * 3, codes
        r = await c.get("/api/data", headers=H); assert r.headers.get("Retry-After") == "30"
        assert (await c.get("/api/data", headers={"X-Init": init(7, "evil_user")})).status == 403      # посторонние не занимают лимит владельца
        bot._hits.clear(); bot.RATE_LIMIT = 120
        ok("лимит запросов: после порога → 429 + Retry-After, считается по аккаунту")
        assert (await c.get("/api/data", headers=H)).status == 200
asyncio.run(main())
print("\nALL API TESTS PASSED")
if not _db_existed:
    import shutil; shutil.rmtree(os.path.join(ROOT, "data"), ignore_errors=True)
