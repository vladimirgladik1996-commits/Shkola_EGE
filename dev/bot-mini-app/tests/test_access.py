"""Белый список (@ники → Telegram-ID), «пароль один раз» (скользящая сессия), миграция дат.
Прогоняет настоящие aiogram-роутеры бота с подменой сети (в Telegram ничего не уходит)."""
import asyncio, datetime as dt, json, os, sqlite3, sys, time, hmac, hashlib
from urllib.parse import urlencode
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", AUTH_LOGIN="Admin", PUBLIC_URL="https://bot.example.com")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
_db_existed = os.path.exists(os.path.join(ROOT, "data", "tutor.db"))

import auth, bot
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, Update, User

# изолированная БД в памяти (не трогаем data/tutor.db)
mem = sqlite3.connect(":memory:", check_same_thread=False); bot.db.backup(mem); mem.row_factory = sqlite3.Row
bot.db = mem; auth.init(mem)
auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
def ok(c): print("  ✓", c)

assert auth.USERNAMES == {"gladik_vladimir", "gladik_n", "hungerrr"}, auth.USERNAMES
assert auth.ALLOWED_IDS == set()
ok("по умолчанию допущены ровно три ника: Gladik_Vladimir, Gladik_N, hungerrr")

# ---------- fake Telegram ----------
calls = []
class Fake(BaseSession):
    async def close(self): pass
    async def stream_content(self, *a, **k): yield b""
    async def make_request(self, bot_, method, timeout=None):
        calls.append(method)
        return Message(message_id=len(calls), date=dt.datetime.now(), chat=Chat(id=1, type="private"), text="x")
tb = Bot("123456:TESTTOKEN", session=Fake())
dp = Dispatcher(); dp.include_router(bot.pub); dp.include_router(bot.r)
uid_seq = [0]
async def say(uid, username, text):
    uid_seq[0] += 1; calls.clear()
    m = Message(message_id=uid_seq[0], date=dt.datetime.now(), chat=Chat(id=uid, type="private"), text=text,
                from_user=User(id=uid, is_bot=False, first_name="T", username=username))
    await dp.feed_update(tb, Update(update_id=uid_seq[0], message=m))
    return list(calls)
def link_of(calls_):
    for c in calls_:
        kb = getattr(c, "reply_markup", None)
        for row in getattr(kb, "inline_keyboard", []) or []:
            for b in row:
                if b.url: return b.url
async def login(uid, username):
    c = await say(uid, username, "/start"); url = link_of(c); assert url and url.startswith("https://bot.example.com/login#"), url
    code, msg, tid = await auth.attempt(url.split("#")[1], "Admin", "Tst-Only-Pw-7qZ4!")
    assert code == 200 and tid == uid, (code, msg)

def init_data(uid, username=None, token="123456:TESTTOKEN"):
    u = {"id": uid, "first_name": "T"}
    if username: u["username"] = username
    d = {"auth_date": str(int(time.time())), "user": json.dumps(u), "query_id": "A"}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)
class Rq:
    def __init__(s, x): s.headers = {"X-Init": x}

async def main():
    # 1. чужие: бот молчит полностью
    for uid, un in ((900, "random_guy"), (901, None), (902, "gladik_vladimir_x"), (903, "Gladik_Vlad")):
        assert await say(uid, un, "/start") == [], (uid, un)
        assert await say(uid, un, "Занятия сегодня") == [], (uid, un)
    ok("посторонние (в т.ч. похожие ники и без ника): /start и кнопки — без ответа, ссылка не выдаётся")
    assert bot.authorized(Rq(init_data(900, "random_guy"))) == 403 and bot.authorized(Rq(init_data(901))) == 403
    ok("API: посторонний с валидной подписью → 403")

    # 2. свой по нику, но без пароля: ссылка выдаётся, меню закрыто, API → 401
    for uid, un in ((101, "Gladik_Vladimir"), (102, "gladik_n"), (103, "HUNGERRR")):
        c = await say(uid, un, "/start"); assert link_of(c), un
        c = await say(uid, un, "Занятия сегодня"); assert len(c) == 1 and "войд" in c[0].text.lower(), un   # только просьба войти
        assert bot.authorized(Rq(init_data(uid, un))) == 401
    ok("трое по нику получают ссылку; до пароля меню закрыто, API → 401 (без регистра ника)")

    # 3. ПАРОЛЬ ОДИН РАЗ: после входа /start, кнопки и API работают без повторного входа
    for uid, un in ((101, "Gladik_Vladimir"), (102, "Gladik_N"), (103, "hungerrr")): await login(uid, un)
    for uid, un in ((101, "Gladik_Vladimir"), (102, "Gladik_N"), (103, "hungerrr")):
        c = await say(uid, un, "/start"); assert link_of(c) is None and any("Готово" in (getattr(x, "text", "") or "") for x in c), un
        c = await say(uid, un, "Занятия сегодня"); assert c and not any("войд" in (getattr(x, "text", "") or "").lower() for x in c)
        assert bot.authorized(Rq(init_data(uid, un))) == 200
    ok("после одного входа: повторный /start пароль не просит, меню и кабинет открыты у всех троих")
    pins = dict(mem.execute("select username, tg_id from auth_pins").fetchall())
    assert pins == {"gladik_vladimir": 101, "gladik_n": 102, "hungerrr": 103}, pins
    ok("ники закреплены за Telegram-ID после первого входа")

    # 4. захват ника: ID другой → отказ, даже с верной подписью; владелец после смены ника всё ещё свой
    assert await say(555, "Gladik_N", "/start") == []
    assert bot.authorized(Rq(init_data(555, "Gladik_N"))) == 403
    assert await say(102, "totally_new_nick", "Занятия сегодня")          # тот же человек, другой ник
    assert bot.authorized(Rq(init_data(102, "totally_new_nick"))) == 200
    ok("чужой ID с ником из списка → отказ; свой ID после смены ника → доступ сохранён")

    # 5. сессия скользящая: пока пользуются — не истекает; без активности — истекает
    now = time.time()
    mem.execute("update auth_sessions set exp=? where tg_id=101", (now + 3 * 86400,)); mem.commit()
    assert auth.is_authorized(101)
    exp = mem.execute("select exp from auth_sessions where tg_id=101").fetchone()[0]
    assert exp > now + auth.SESSION_SEC - 120, "сессия не продлилась"
    mem.execute("update auth_sessions set exp=? where tg_id=101", (time.time() - 1,)); mem.commit()
    assert not auth.is_authorized(101) and bot.authorized(Rq(init_data(101, "Gladik_Vladimir"))) == 401
    ok(f"сессия продлевается при использовании (окно {auth.SESSION_SEC // 86400} дн. без активности); просроченная → вход заново")

    # 6. убрали человека из списка → сессия мгновенно мертва; смена пароля — тоже
    saved = set(auth.USERNAMES); auth.USERNAMES.discard("hungerrr")
    assert not auth.is_authorized(103) and await say(103, "hungerrr", "Занятия сегодня") == []
    auth.USERNAMES.add("hungerrr"); assert auth.is_authorized(103)
    old = auth.PW_HASH; auth.PW_HASH = auth.make_hash("Another-Strong-Pass-2026!")
    assert not auth.is_authorized(103) and not auth.is_authorized(102); auth.PW_HASH = old
    ok("удалили ник из ALLOWED_USERNAMES → доступ закрыт сразу; смена пароля → все сессии недействительны")

    # 7. ссылка, выданная до исключения из списка, не работает
    c = await say(102, "Gladik_N", "/start")      # сессия 102 жива → ссылку не выдаст; удалим сессию
    auth.logout(102); c = await say(102, "Gladik_N", "/start"); url = link_of(c); assert url
    auth.USERNAMES.discard("gladik_n"); auth.ALLOWED_IDS.discard(102)
    mem.execute("delete from auth_pins where tg_id=102"); mem.commit()
    code, msg, _ = await auth.attempt(url.split("#")[1], "Admin", "Tst-Only-Pw-7qZ4!"); assert code == 403, code
    auth.USERNAMES.clear(); auth.USERNAMES.update(saved)
    ok("ссылка на вход, выданная до исключения человека из списка, → 403")

    # 8. ALLOWED_IDS: допуск по числовому ID без ника
    auth.ALLOWED_IDS.add(777); assert await say(777, None, "/start") and auth.allowed(777, None); auth.ALLOWED_IDS.discard(777)
    ok("числовой ALLOWED_IDS/OWNER_ID работает без ника")

    # 9. миграция старых дат
    mem.execute("insert into students(id,name) values(1,'t')")
    mem.execute("insert into lessons(student_id,day,time) values(1,'20261005','09:00')")
    mem.execute("insert into lessons(student_id,day,time) values(1,'2026-W41-2','09:00')")
    mem.commit(); bot._canon_days()
    days = sorted(r[0] for r in mem.execute("select day from lessons"))
    assert days == ["2026-10-05", "2026-10-06"], days
    ok("старые записи с днём «20261005»/«2026-W41-2» приведены к YYYY-MM-DD")
    print("\nALL ACCESS TESTS PASSED")

try:
    asyncio.run(main())
finally:
    if not _db_existed:
        import shutil; shutil.rmtree(os.path.join(ROOT, "data"), ignore_errors=True)
