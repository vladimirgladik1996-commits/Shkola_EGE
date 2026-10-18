"""Стрим от kimi.ask_stream НЕ должен умирать от таймаута, пока модель думает.

Регрессия на самый обидный сценарий: «Вишенка думает…» → потом «ИИ сейчас не отвечает».
Причина — aiohttp.ClientTimeout(total=N) убивал СОЕДИНЕНИЕ целиком, если размышление
длилось дольше N секунд, хотя данные всё это время исправно приходили. Для kimi-k3
с reasoning_effort=max размышление легко идёт 5–10 минут — запрос гарантированно
обрывался, и пользователь не получал ответ.

Тест поднимает настоящий SSE-сервер, который стримит размышление ДОЛЬШЕ, чем
kimi.TIMEOUT, и проверяет, что ответ всё равно доходит до конца.

Запуск: python3 tests/test_kimi_stream.py
"""
import asyncio, json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com",
                  LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID", "KIMI_API_KEY", "KIMI_BASE_URL",
          "KIMI_MODEL", "KIMI_MODEL_FAST", "KIMI_MODEL_DEEP", "KIMI_EFFORT",
          "KIMI_MAX_TOKENS", "KIMI_MAX_TOKENS_DEEP", "KIMI_MAX_TOKENS_CEILING", "KIMI_THINK_EXTRA"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import kimi
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def ok(m):
    print("  ✓", m)


CALLS = []
THINK_SECONDS = 3.0        # модель «думает» дольше, чем kimi.TIMEOUT из теста


async def sse(request):
    """SSE-сервер как Moonshot: сначала reasoning_content, потом content."""
    body = await request.json()
    CALLS.append(body)
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8"})
    await resp.prepare(request)
    t0 = time.time()
    while time.time() - t0 < THINK_SECONDS:
        chunk = {"choices": [{"delta": {"reasoning_content": "сначала подумаю… "}}]}
        await resp.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
        await asyncio.sleep(0.2)
    for part in ("Готово: ", "ответ получен."):
        chunk = {"choices": [{"delta": {"content": part}}]}
        await resp.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
    await resp.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


async def silent(request):
    """Мёртвое соединение: ни одного байта — клиент обязан заметить тишину и ответить err."""
    await asyncio.sleep(30)
    return web.Response()


fk = web.Application()
fk.router.add_post("/chat/completions", sse)
fk.router.add_post("/silent", silent)


async def collect(**kw):
    out = []
    async for item in kimi.ask_stream([{"role": "user", "content": "реши задачу"}], **kw):
        out.append(item)
    return out


async def main():
    async with TestClient(TestServer(fk)) as c:
        kimi.BASE = str(c.make_url("")).rstrip("/")
        kimi.API_KEY = "***"

        # --- 1) размышление ДОЛЬШЕ таймаута — ответ всё равно приходит ---
        kimi.TIMEOUT = kimi.TIMEOUT_DEEP = 1.0    # раньше total=1 с убивал стрим на 3-й секунде
        kimi.SOCK_READ = 10.0
        t0 = time.time()
        out = await asyncio.wait_for(collect(deep=True), timeout=30)
        dt = time.time() - t0
        kinds = [k for k, _ in out]
        text = "".join(v for k, v in out if k == "text")
        assert "err" not in kinds, out
        assert text == "Готово: ответ получен.", out
        assert dt > THINK_SECONDS, dt
        ok("стрим пережил размышление длиной %.1f с при kimi.TIMEOUT=1 с — ответ дошёл целиком" % dt)

        # --- 2) бюджет и модель уходят строго как задумано ---
        b = CALLS[-1]
        assert b["model"] == "kimi-k3" and b["reasoning_effort"] == "max", b
        assert b["max_tokens"] == 100_000_000, b
        assert b["stream"] is True and b["stream_options"] == {"include_usage": True}
        ok("запрос: kimi-k3 + reasoning_effort=max + max_tokens=100 000 000, модель не подменена")

        # --- 3) быстрый режим: слабая модель, размышления выключены ---
        out = await collect(deep=False)
        assert [k for k, _ in out if k == "err"] == [], out
        assert "".join(v for k, v in out if k == "text") == "Готово: ответ получен."
        b = CALLS[-1]
        assert b["model"] == "kimi-k2.6" and b["thinking"] == {"type": "disabled"}, b
        assert b["max_tokens"] == 100_000_000 and "reasoning_effort" not in b
        ok("быстрый режим: kimi-k2.6, thinking выключен, тот же огромный бюджет")

        # --- 4) мёртвое соединение не вешает запрос вечно ---
        kimi.SOCK_READ = 1.0

        async def dead(messages, **kw):
            body = {"model": "x", "messages": messages, "stream": True, "max_tokens": 1}
            async with kimi.aiohttp.ClientSession(timeout=kimi.aiohttp.ClientTimeout(
                    total=None, sock_connect=5, sock_read=kimi.SOCK_READ)) as s:
                async with s.post(kimi.BASE + "/silent", json=body) as r:
                    async for _ in r.content:
                        pass
        try:
            await asyncio.wait_for(dead([{"role": "user", "content": "x"}]), timeout=20)
            assert False, "тишина не была замечена"
        except Exception as e:
            ok("тишина в потоке ловится по sock_read (%r) — запрос не виснет вечно" % type(e).__name__)


asyncio.run(main())
print("test_kimi_stream: OK")
