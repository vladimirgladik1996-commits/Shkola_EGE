"""Резервное копирование и восстановление: консистентный снимок базы, живая подмена файла без
рестарта, отказ от мусора, «пустую базу не рассылаем», Cache-Control: no-store у страницы и API."""
import asyncio, os, sqlite3, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", AUTH_LOGIN="Admin", PUBLIC_URL="https://bot.example.com")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import auth, bot

def ok(c): print("  ✓", c)

# изолированная БД: не трогаем рабочую data/tutor.db
TEST_DB = os.path.join(ROOT, "data", "tutor-test.db")
for s in ("", "-wal", "-shm"):
    p = TEST_DB + s
    if os.path.exists(p): os.remove(p)
bot.DB_PATH = TEST_DB
bot.db.close()
bot.db = sqlite3.connect(TEST_DB, check_same_thread=False)
bot.db.row_factory = sqlite3.Row
bot.init_db(bot.db); auth.init(bot.db)

# ---------- 1. мусор вместо файла базы отвергается, рабочая база цела ----------
for bad in (b"", "просто текст".encode(), b"SQLite format 3\x00garbage-not-a-db"):
    try:
        bot.restore_db(bad)
        raise AssertionError("мусор прошёл проверку")
    except Exception:
        pass
assert bot.q("select 1 from students limit 1") == []
ok("не-SQLite и повреждённый файл отклоняются, текущая база не тронута")

# ---------- 2. снимок и живое восстановление ----------
sid = bot.run("insert into students(name,price,grade) values('Тест Ученик',1500,'11')")
bot.run("insert or replace into auth_pins(username,tg_id) values('gladik_vladimir',4242)")
bot.run("insert or replace into auth_sessions(tg_id,exp,fp) values(4242,?,?)", time.time() + 3600, auth._fp())
snap = bot.db_snapshot()
assert isinstance(snap, bytes) and snap.startswith(b"SQLite format 3\x00")
ok("db_snapshot отдаёт консистентный файл SQLite (backup API)")

for t in ("students", "lessons", "payments", "auth_sessions", "auth_pins"):   # имитация рестарта контейнера
    bot.db.execute(f"delete from {t}")
bot.db.commit()
assert not bot.q("select 1 from students limit 1") and auth.is_authorized(4242) is False
ok("после «рестарта» данных и входа нет")

bot.restore_db(snap)
assert bot.q("select name from students where id=?", sid)[0]["name"] == "Тест Ученик"
assert auth.is_authorized(4242) is True
ok("restore_db вернул данные и сессию входа без перезапуска процесса")

# ---------- 3. пустая база не считается поводом для бэкапа ----------
assert bot._has_data() is True
for t in ("students", "payments"):
    bot.db.execute(f"delete from {t}")
bot.db.commit()
assert bot._has_data() is False
ok("пустая база не рассылается как бэкап (не затрёт последний нормальный файл в чате)")

# ---------- 4. страница и API не кешируются ----------
async def http():
    from aiohttp.test_utils import TestClient, TestServer
    cl = TestClient(TestServer(bot.make_app()))
    await cl.start_server()
    r1 = await cl.get("/")
    assert r1.status == 200 and r1.headers.get("Cache-Control") == "no-store", (r1.status, dict(r1.headers))
    r2 = await cl.get("/api/data")
    assert r2.status == 403 and r2.headers.get("Cache-Control") == "no-store", (r2.status, dict(r2.headers))
    await cl.close()
asyncio.run(http())
ok("и страница, и API отдаются с Cache-Control: no-store (WebView не принесёт старый фронт)")

for s in ("", "-wal", "-shm"):
    p = TEST_DB + s
    if os.path.exists(p): os.remove(p)
print("ALL BACKUP TESTS PASSED")
