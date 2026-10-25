"""Регрессия NDJSON-стримов: сбой ПОСЛЕ resp.prepare() не «дописывает» второй HTTP-статус.
Было: пайплайн падал (напр. TypeError: 'int' object is not iterable) → guarded возвращал
web.Response(400) ПОВЕРХ начатого chunked-тела → клиент получал
«Invalid character in chunk size: b'HTTP/1.1 400 Bad Request'», reverse-proxy Render
считал бэкенд мёртвым. Стало: {"t":"fail"} + штатный конец тела.
Причина сбоя различается (баг-репорт 010): падение пайплайна на сервере → "srv",
недоступный сервис модели/OCR → "net"/"http"/"empty".
Страдали оба стрима: /api/essay_check/run и /api/chat. Запуск: python3 tests/test_stream_fail.py"""
import asyncio, hashlib, hmac, json, os, sqlite3, sys, time
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
_db_existed = os.path.exists(os.path.join(ROOT, "data", "tutor.db"))
import auth, bot, assist, essaycheck, kimi, yavision
from aiohttp.test_utils import TestClient, TestServer

def ok(c): print("  ✓", c)

mem = sqlite3.connect(":memory:", check_same_thread=False)
bot.init_db(mem)
bot.db = mem; auth.init(mem); auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp())); mem.commit()

def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)

H = {"X-Init": init(42, "Gladik_N")}
kimi.enabled = lambda: True            # тестовый стенд: ключи «есть»
yavision.enabled = lambda: True

def ndjson(raw):
    """Тело стрима обязано быть чистым NDJSON: строка второго статуса ломала бы chunked-парсер."""
    assert b"HTTP/1.1" not in raw, "в теле стрима второй HTTP-статус: %r" % raw[:120]
    return [json.loads(ln) for ln in raw.decode("utf-8").splitlines() if ln.strip()]

async def main():
    async with TestClient(TestServer(bot.make_app())) as c:
        # --- 1. /api/essay_check/run: пайплайн падает ПОСЛЕ начала стрима (как в проде) ---
        async def boom_run(*a, **k):
            yield "status", "Собрала фото"
            raise TypeError("'int' object is not iterable")   # ровно то, что падало в _bind_coords
            yield
        essaycheck.run, real_run = boom_run, essaycheck.run
        bot.EC_CHECKS["cid1"] = [42, {"w": ["x"], "t": []}, time.time()]
        r = await c.post("/api/essay_check/run", json={"id": "cid1"}, headers=H)
        assert r.status == 200 and "ndjson" in r.headers["Content-Type"], (r.status, r.headers)
        evs = ndjson(await r.read())
        assert evs[0] == {"t": "status", "v": "Собрала фото"}, evs
        assert evs[-1] == {"t": "fail", "why": "srv"}, evs
        ok("/api/essay_check/run: сбой после prepare() → 200 + {\"t\":\"fail\"} + валидный конец стрима")

        # --- 2. /api/chat: тот же дефект был и здесь ---
        async def boom_chat(*a, **k):
            yield "status", "read"
            raise TypeError("'int' object is not iterable")
            yield
        assist.chat_stream, real_chat = boom_chat, assist.chat_stream
        r = await c.post("/api/chat", json={"messages": [{"role": "user", "text": "привет"}]}, headers=H)
        assert r.status == 200 and "ndjson" in r.headers["Content-Type"], (r.status, r.headers)
        evs = ndjson(await r.read())
        assert evs[0] == {"t": "status", "v": "read"} and evs[-1] == {"t": "fail", "why": "net"}, evs
        ok("/api/chat: сбой после prepare() → 200 + {\"t\":\"fail\"} + валидный конец стрима")

        # --- 3. обычные ошибки ДО стрима — по-прежнему простой HTTP-код, без событий ---
        r = await c.post("/api/essay_check/run", json={"id": "нет такого"}, headers=H)
        assert r.status == 400, r.status
        r = await c.post("/api/chat", json={"messages": "мусор"}, headers=H)
        assert r.status == 400, r.status
        ok("ошибка до начала стрима: обычный 400 (контракт изменился только для начатого стрима)")

        # --- 4. честный сбой этапа по своей воле (err) — fail с точной причиной ---
        async def err_run(*a, **k):
            yield "status", "Распознан рукописный текст"
            yield "err", "http"
            yield
        essaycheck.run = err_run
        bot.EC_CHECKS["cid2"] = [42, {"w": ["x"], "t": []}, time.time()]
        r = await c.post("/api/essay_check/run", json={"id": "cid2"}, headers=H)
        evs = ndjson(await r.read())
        assert evs[-1] == {"t": "fail", "why": "http"}, evs
        ok("сбой этапа (err): fail/why=http — фронт различает «ИИ не отвечает» и «сбой разбора»")
        essaycheck.run, assist.chat_stream = real_run, real_chat

    print("test_stream_fail: OK")

asyncio.run(main())
if not _db_existed:
    import shutil
    shutil.rmtree(os.path.join(ROOT, "data"), ignore_errors=True)
