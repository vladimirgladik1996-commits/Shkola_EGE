"""«ИИ-проверка»: обрыв соединения больше не съедает проверку (баг-репорт 010).

Симптом из прод-логов: проверка идёт 3–5 минут одним HTTP-потоком, соединение на мобильной
сети рвётся — в логах «api: клиент отключился» и "POST /api/essay_check/run" 200 с НУЛЁМ байт.
Раньше вся работа (OCR + анализ Кими) выбрасывалась вместе с обрывом (task.cancel в finally),
и пользователь вместо разбора получал пустое превью с текстом «Прервалась связь с сервером —
проверьте интернет», хотя сервер был исправен.

Стало: события и итог копятся в bot.EC_JOBS[cid] («журнал сеанса»), а стрим только
ПОДКЛЮЧАЕТСЯ к журналу. Обрыв не трогает ни проверку, ни журнал; «Повторить» доигрывает
уже сделанное и продолжает с места обрыва. Журнал удаляется, только когда итог отдан
клиенту, — тогда повторный run по сеансу начинает новую проверку.

Запуск: python3 tests/test_essay_resume.py
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
import auth, bot, essaycheck, kimi, yavision
from aiohttp.test_utils import TestServer


def ok(c):
    print("  ✓", c)


html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()

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
yavision.enabled = lambda: True

# ---------- 1. контракт фронта: обрыв = «link», повторное подключение к тому же сеансу ----------
assert "const EC_RETRY=" in html, "нет повторных подключений к журналу сеанса после обрыва (EC_RETRY)"
assert 'ecFail("link")' in html, "обрыв соединения должен отличаться от «проверьте интернет» (ecFail(\"link\"))"
assert 'ecWhy==="link"' in html and "Работа не потеряна" in html, \
    "для «link» нужен свой текст: работа не потеряна, «Повторить» продолжит с места обрыва"
assert 'ecWhy==="srv"' in html, "для «srv» (сбой на сервере) нужен свой текст ошибки"
run_body = html[html.index("async function ecRun(){"):html.index("function ecRetry(")]
assert "for(let a=0;a<=EC_RETRY;a++)" in run_body, "ecRun обязан переподключаться к той же проверке"
assert 'ecLive=[];ecWait=EC_STAGES.slice();ecThink=""' in run_body, \
    "журнал сеанса приходит с начала — экран пересобирается заново на каждом подключении"
assert "ecLive.indexOf(ev.v)<0" in run_body, \
    "статусы из журнала не должны дублироваться в ecLive при повторном подключении"
ok("фронт: обрыв → ecFail(\"link\") и переподключение к журналу сеанса (EC_RETRY)")

# ---------- 2. живое поведение: обрыв клиента не убивает проверку, «Повторить» доигрывает журнал ----------
async def main():
    real_run, real_beat, real_timeout = essaycheck.run, bot.EC_BEAT, bot.EC_TIMEOUT
    bot.EC_BEAT, bot.EC_TIMEOUT = 0.2, 10.0
    gate = asyncio.Event()                            # «долгий» этап OCR/анализа — держим проверку живой

    async def slow_run(w, t, kind="ege"):
        yield "status", "Собрала фото"
        yield "think", "думаю"
        await gate.wait()
        yield "status", "Сверяю с критериями ФИПИ…"
        yield "done", {"text": "текст", "task": "", "errors": [], "criteria": [],
                       "recommendations": [], "score": {"got": 0, "max": 0, "percent": 0, "level": "Отлично"}}

    essaycheck.run = slow_run
    server = TestServer(bot.make_app())
    await server.start_server()
    url = str(server.make_url("/api/essay_check/run"))
    bot.EC_CHECKS["cid-resume"] = [42, {"w": ["x"], "t": []}, time.time()]

    try:
        # --- 2а. клиент получает первые события и ОБРЫВАЕТ соединение (как мобильная сеть) ---
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={"id": "cid-resume"}, headers=H) as r:
                head = await r.content.read(64)
                assert r.status == 200 and head, (r.status, head)
            # выход из контекста — соединение закрыто посреди стрима
        await asyncio.sleep(0.8)                      # даём серверу заметить обрыв

        job = bot.EC_JOBS.get("cid-resume")
        assert job is not None, \
            "журнал сеанса исчез при обрыве клиента — проверка снова потеряна (баг-репорт 010)"
        assert not job["final"], "проверка ещё идёт — итога быть не должно"
        assert job["task"] is not None and not job["task"].done(), \
            "проверка остановлена обрывом клиента — вся работа выброшена (баг-репорт 010)"
        ok("обрыв клиента: проверка продолжается, журнал сеанса цел")

        # --- 2б. «Повторить»: подключаемся к тому же сеансу — журнал доигрывается с начала ---
        gate.set()                                    # «долгий» этап закончился
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={"id": "cid-resume"}, headers=H) as r:
                evs = [json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
        assert evs[0] == {"t": "status", "v": "Собрала фото"}, evs
        assert evs[-1]["t"] == "done" and evs[-1]["result"]["text"] == "текст", evs
        assert [e["t"] for e in evs].count("status") == 2, f"статусы задублировались: {evs}"
        assert not any(e.get("t") == "fail" for e in evs), evs
        ok("«Повторить»: журнал доигрывается с начала — разбор приходит без повторного ожидания")

        # --- 2в. итог отдан → журнал подчищен, повторный run начинает НОВУЮ проверку ---
        assert "cid-resume" not in bot.EC_JOBS, "журнал не подчищен после отдачи итога"
        gate.clear()
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={"id": "cid-resume"}, headers=H) as r:
                chunk = await r.content.read(32)
                assert chunk, "новый запуск не отдаёт события"
            # обрываем снова: проверяем, что это НОВЫЙ журнал, а не старый итог
        await asyncio.sleep(0.5)
        job = bot.EC_JOBS.get("cid-resume")
        assert job is not None and not job["final"], f"повторный run не начал новую проверку: {job}"
        gate.set()
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={"id": "cid-resume"}, headers=H) as r:
                evs = [json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
        assert evs[-1]["t"] == "done", evs
        ok("итог отдан — журнал подчищен, следующий запуск по сеансу = новая проверка")
    finally:
        gate.set()
        await server.close()
        essaycheck.run, bot.EC_BEAT, bot.EC_TIMEOUT = real_run, real_beat, real_timeout

    print("test_essay_resume: OK")


asyncio.run(main())
if not _db_existed:
    shutil.rmtree(os.path.join(ROOT, "data"), ignore_errors=True)
