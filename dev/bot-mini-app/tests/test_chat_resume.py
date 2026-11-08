"""«Чат с ИИ»: обрыв соединения больше не съедает ответ (баг-репорт 019).

Симптом из АПК: при создании PDF через чат клиент показывал «Не получилось получить ответ.
Проверьте связь», хотя связь была стабильна и файл приходил в Telegram-бота. Причина —
конвейер ответа (модель → сборка файла → проверка Кими → отправка в Telegram) идёт минутами
ОДНИМ HTTP-потоком; на мобильной сети такое соединение рвётся посреди работы (тот же класс,
что «api: клиент отключился» в баг-репорте 010 для «ИИ-проверки»).

Стало: события и итог копятся в bot.CHAT_JOBS[rid] («журнал запроса»), а стрим только
ПОДКЛЮЧАЕТСЯ к журналу. Обрыв не трогает ни конвейер, ни журнал; «Повторить» (тот же rid)
доигрывает журнал с начала и продолжает с места обрыва. Журнал удаляется, когда итог отдан
клиенту, — тогда следующий запрос начинает новый конвейер. Падение конвейера — честный
{"t":"fail","why":"srv"}, а не молчаливый обрыв соединения.

Запуск: python3 tests/test_chat_resume.py
"""
import asyncio, hashlib, hmac, json, os, shutil, sqlite3, sys, time
from urllib.parse import urlencode

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
_db_existed = os.path.exists(os.path.join(ROOT, "data", "tutor.db"))
import assist, auth, bot, kimi
from aiohttp.test_utils import TestServer


def ok(c):
    print("  ✓", c)


html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()

# ---------- 1. контракт фронта: обрыв = переподключение к журналу, честные тексты ----------
assert "chAsk.rid" in html and "files,rid" in html, \
    "запрос чата обязан нести rid — иначе журналу не с чем переподключиться"
assert "const retry=" in html and "CH.splice(base)" in html, \
    "нет переподключения к журналу запроса после обрыва (retry + доигрывание с начала)"
assert "работа не потеряна" in html, \
    "для обрыва свой текст: работа не потеряна, «Повторить» продолжит с места обрыва"
assert 'ev.why=="srv"' in html, "для «srv» (сбой на сервере) нужен свой текст ошибки"
assert "chAsk(true)" in html, "«Повторить» должен подключаться к журналу того же запроса (chAsk(true))"
assert "CHAT_JOBS" in src and "_chat_jobs_gc" in src and '"why": "srv"' in src, \
    "сервер хранит журнал запроса (CHAT_JOBS) и честно отвечает srv при падении конвейера"
ok("фронт+бэк: rid, переподключение к журналу, честные тексты обрыва и «srv»")

# --- возврат в мини-апп к идущему ответу: стрим продолжается вживую (баг-репорт 025) ---
assert "localStorage.chRid" in html and "chAsk(true,true)" in html and "resume:!!attachOnly" in html, \
    "возврат в мини-апп обязан подключаться к журналу идущего запроса (chRid в памяти + resume)"
assert 'ev.t=="gone"' in html and 'resume = j.get("resume"' in src and '{"t":"gone"}' in src, \
    "исчезнувший журнал — честное «gone» (клиент и сервер), а не перезапуск задачи моделью"
ok("возврат в мини-апп: rid переживает перезагрузку страницы, подключение без перезапуска конвейера")

# ---------- 2. живое поведение: обрыв клиента не убивает конвейер, «Повторить» доигрывает журнал ----------
async def main():
    gate = asyncio.Event()                            # «долгий» этап конвейера — держим запрос живым

    async def slow_stream(hist, deep=False, images=None, docs=None, fmt="text", img_names=None, mat=""):
        yield "status", "think"
        yield "think", "мысль"
        await gate.wait()
        yield "status", "write"
        yield "text", "ответ"
        yield "done", "ответ"

    real_stream = assist.chat_stream
    assist.chat_stream = slow_stream

    class FakeBot:
        def __init__(self):
            self.msgs = []

        async def send_message(self, chat_id, text, **kw):
            self.msgs.append((chat_id, text))

        async def send_document(self, chat_id, document, caption=None, **kw):
            self.msgs.append((chat_id, caption))

        async def send_photo(self, chat_id, photo, caption=None, **kw):
            self.msgs.append((chat_id, caption))

    app = bot.make_app()
    app["bot"] = FakeBot()
    server = TestServer(app)
    await server.start_server()
    url = str(server.make_url("/api/chat"))
    body = {"messages": [{"role": "user", "text": "привет"}], "rid": "rid-resume-01"}
    try:
        # --- 2а. клиент получает первые события и ОБРЫВАЕТ соединение (как мобильная сеть) ---
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body, headers=H) as r:
                head = await r.content.read(64)
                assert r.status == 200 and head, (r.status, head)
            # выход из контекста — соединение закрыто посреди стрима
        await asyncio.sleep(0.8)                      # даём серверу заметить обрыв

        job = bot.CHAT_JOBS.get("rid-resume-01")
        assert job is not None, "журнал запроса исчез при обрыве клиента — ответ снова потерян (баг-репорт 019)"
        assert not job["final"], "конвейер ещё идёт — итога быть не должно"
        assert job["task"] is not None and not job["task"].done(), \
            "конвейер остановлен обрывом клиента — вся работа выброшена (баг-репорт 019)"
        ok("обрыв клиента: конвейер продолжается, журнал запроса цел")

        # --- 2б. «Повторить» (тот же rid): журнал доигрывается с начала — ответ приходит целиком ---
        gate.set()
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body, headers=H) as r:
                evs = [json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
        evs = [e for e in evs if e.get("t") != "hb"]
        assert [e["t"] for e in evs] == ["status", "think", "status", "text", "done", "tg"], evs
        assert evs[3]["v"] == "ответ" and evs[4]["reply"] == "ответ" and evs[4]["enabled"] is True, evs
        assert app["bot"].msgs == [(42, "ответ")], app["bot"].msgs
        ok("«Повторить»: журнал доигрывается с начала — ответ приходит и в чат, и в Telegram")

        # --- 2в. итог отдан → журнал подчищен; тот же rid начинает НОВЫЙ конвейер ---
        assert "rid-resume-01" not in bot.CHAT_JOBS, "журнал не подчищен после отдачи итога"
        gate.clear()
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body, headers=H) as r:
                chunk = await r.content.read(32)
                assert chunk, "новый запрос не отдаёт события"
        await asyncio.sleep(0.5)
        job = bot.CHAT_JOBS.get("rid-resume-01")
        assert job is not None and not job["final"], f"запрос после отдачи итога не начался заново: {job}"
        gate.set()
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body, headers=H) as r:
                evs = [json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
        assert [e for e in evs if e.get("t") != "hb"][-1]["t"] in ("done", "tg"), evs
        ok("итог отдан — журнал подчищен, следующий запрос = новый конвейер")

        # --- 2г. падение конвейера: честный fail/srv, а не молчаливый обрыв ---
        async def boom(*a, **kw):
            yield "status", "think"
            raise RuntimeError("намеренное падение")
            yield

        assist.chat_stream = boom
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={"messages": [{"role": "user", "text": "привет"}], "rid": "rid-boom-01"},
                              headers=H) as r:
                evs = [json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
        evs = [e for e in evs if e.get("t") != "hb"]
        assert evs[-1] == {"t": "fail", "why": "srv"}, evs
        assert "rid-boom-01" not in bot.CHAT_JOBS, "журнал упавшего запроса не подчищен"
        ok("падение конвейера → честный fail/srv в стриме, без «проверьте связь»")

        # --- 2д. возврат в мини-апп (resume=true): только подключение — конвейер не перезапускается ---
        calls = []

        async def counting(*a, **kw):
            calls.append(1)
            yield "status", "think"
            yield "think", "мысль"
            await gate.wait()
            yield "done", "ответ"

        assist.chat_stream = counting
        gate.clear()
        rb = {"messages": [{"role": "user", "text": "привет"}], "rid": "rid-back-01"}
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=rb, headers=H) as r:
                await r.content.read(32)                       # первые события ушли — клиент «вышел» из мини-аппа
        await asyncio.sleep(0.3)
        assert bot.CHAT_JOBS.get("rid-back-01") is not None and not bot.CHAT_JOBS["rid-back-01"]["final"], \
            "журнал идущего запроса должен ждать возврата клиента"
        gate.set()                                             # конвейер дошёл до итога, пока клиента нет
        await asyncio.sleep(0.3)
        async with aiohttp.ClientSession() as s:               # вернулись: resume=true — только подключиться
            async with s.post(url, json={**rb, "resume": True}, headers=H) as r:
                evs = [json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
        evs = [e for e in evs if e.get("t") != "hb"]
        assert [e["t"] for e in evs] == ["status", "think", "done", "tg"], evs   # журнал доигран целиком
        assert len(calls) == 1, f"resume перезапустил конвейер (вызовов {len(calls)}) — модель переделала задачу"
        assert "rid-back-01" not in bot.CHAT_JOBS, "итог отдан — журнал подчищен"
        ok("возврат в мини-апп: журнал доигрывается вживую, конвейер НЕ перезапускается")

        # --- 2е. resume без журнала (итог отдан/устарел): «gone», новый конвейер не запускается ---
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={"messages": [{"role": "user", "text": "привет"}],
                                         "rid": "rid-gone-01", "resume": True}, headers=H) as r:
                evs = [json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
        assert evs == [{"t": "gone"}], evs
        assert len(calls) == 1 and "rid-gone-01" not in bot.CHAT_JOBS, (calls, list(bot.CHAT_JOBS))
        ok("resume без журнала → честное «gone» без запуска нового конвейера")
    finally:
        gate.set()
        await server.close()
        assist.chat_stream = real_stream

    print("test_chat_resume: OK")


mem = sqlite3.connect(":memory:", check_same_thread=False)
bot.init_db(mem)
bot.db = mem
auth.init(mem)
auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp()))
mem.commit()


def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest(),
                         chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)


H = {"X-Init": init(42, "Gladik_N")}
kimi.enabled = lambda: True

asyncio.run(main())
if not _db_existed:
    shutil.rmtree(os.path.join(ROOT, "data"), ignore_errors=True)
