"""Обрезанный ответ модели НЕ выдаётся за полный (корень бага «в Word попало не всё»).

Регрессия на кейс «тест по произведению»: в файл попали вопросы, но не попали ответы.
Механика: поток обрывается (finish_reason=length — кончился бюджет, обрыв соединения),
часть ответа уже отдана — и раньше она молча выдавалась за готовый ответ, файл собирался
из обрывка. Теперь kimi САМ дособирает ответ продолжением (до kimi.MAX_CONT заходов):
обрезанный текст закрепляется репликой ассистента, модель продолжает с места обрыва —
в ответы и файлы попадает всё, что API выдал целиком.

Запуск: python3 tests/test_kimi_continue.py
"""
import asyncio, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", PUBLIC_URL="https://bot.example.com",
                  LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID", "KIMI_API_KEY", "KIMI_BASE_URL",
          "KIMI_MODEL", "KIMI_MODEL_FAST", "KIMI_MODEL_DEEP", "KIMI_EFFORT",
          "KIMI_MAX_TOKENS", "KIMI_MAX_TOKENS_DEEP", "KIMI_MAX_TOKENS_CEILING", "KIMI_THINK_EXTRA",
          "KIMI_MAX_CONT"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import kimi
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def ok(m):
    print("  ✓", m)


CALLS = []
RESP = []   # очередь ответов сервера:
            # ("sse", [дельты…], finish_reason|None, done?)  или  ("json", content, finish_reason)


async def completions(request):
    body = await request.json()
    CALLS.append(body)
    kind, *rest = RESP.pop(0)
    if kind == "json":
        content, finish = rest
        return web.json_response({"choices": [{"message": {"content": content}, "finish_reason": finish}]})
    deltas, finish, done = rest
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8"})
    await resp.prepare(request)
    for d in deltas:
        await resp.write(("data: " + json.dumps({"choices": [{"delta": d}]}, ensure_ascii=False) + "\n\n").encode())
    if finish:
        await resp.write(("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}) + "\n\n").encode())
    if done:
        await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


app = web.Application()
app.router.add_post("/chat/completions", completions)


async def stream(messages, **kw):
    return [item async for item in kimi.ask_stream(messages, **kw)]


async def main():
    async with TestClient(TestServer(app)) as c:
        kimi.BASE = str(c.make_url("")).rstrip("/")
        kimi.API_KEY = "***"

        # --- 1) стрим обрезан по max_tokens → продолжение → ответ целиком ---
        RESP.append(("sse", [{"reasoning_content": "продумываю ответы… "},
                             {"content": "1. Вопрос один\n"}, {"content": "2. Вопрос два\n"}], "length", True))
        RESP.append(("sse", [{"reasoning_content": "мысль продолжения"},
                             {"content": "Ответы: 1) а 2) б"}], "stop", True))
        out = await stream([{"role": "user", "content": "сделай тест"}], deep=True, thinking=True)
        text = "".join(v for k, v in out if k == "text")
        assert text == "1. Вопрос один\n2. Вопрос два\nОтветы: 1) а 2) б", out
        assert not [k for k, _ in out if k == "err"], out
        assert [k for k, _ in out].count("think") == 1, out   # мышление продолжения не показывается
        ok("стрим, обрезанный по max_tokens, дособран продолжением — ответ целиком")

        # --- 2) продолжение закрепляет обрезанный текст и продолжает с места обрыва ---
        b = CALLS[-1]
        assert b["messages"][-2] == {"role": "assistant", "content": "1. Вопрос один\n2. Вопрос два\n"}, b
        assert b["messages"][-1] == {"role": "user", "content": kimi.CONT_PROMPT}, b
        assert b["model"] == "kimi-k3" and b["reasoning_effort"] == "max", b   # модель и режим не меняются
        ok("продолжение: обрезанный текст закреплён репликой ассистента, модель и режим те же")

        # --- 3) обрыв потока без [DONE] (обрыв сети/прокси) — тоже не «полный ответ» ---
        RESP.append(("sse", [{"content": "Начало ответа. "}], None, False))
        RESP.append(("sse", [{"content": "Конец ответа."}], "stop", True))
        out = await stream([{"role": "user", "content": "вопрос"}])
        assert "".join(v for k, v in out if k == "text") == "Начало ответа. Конец ответа.", out
        ok("обрыв SSE без [DONE] не выдаётся за полный ответ — ответ дособран")

        # --- 4) нестримовый ask(): finish_reason=length → тоже дособирается ---
        RESP.append(("json", "часть первая", "length"))
        RESP.append(("json", " часть вторая", "stop"))
        s = await kimi.ask([{"role": "user", "content": "вопрос"}])
        assert s == "часть первая часть вторая", s
        b = CALLS[-1]
        assert b["messages"][-2] == {"role": "assistant", "content": "часть первая"}, b
        ok("ask(): обрезанный ответ дособирается продолжением, а не выдаётся за готовый")

        # --- 5) целый ответ не трогается: один запрос, продолжений нет ---
        n = len(CALLS)
        RESP.append(("sse", [{"content": "Готово: всё."}], "stop", True))
        out = await stream([{"role": "user", "content": "вопрос"}])
        assert "".join(v for k, v in out if k == "text") == "Готово: всё." and len(CALLS) == n + 1, out
        ok("целый ответ (finish_reason=stop) уходит одним заходом — продолжение не срабатывает")


asyncio.run(main())
print("test_kimi_continue: OK")
