"""Репродукция: что видит пользователь со «стримингом мышления» в запросе «тест в PDF».

Поднимает фейковый SSE-сервер (как Moonshot) в трёх сценариях и считает think-события:
  A) быстрый режим (deep=False): модель kimi-k2.6, thinking=disabled → мышления нет вообще;
  B) глубокий режим, бюджет сгорел на размышлениях → повтор запроса (attempt=1);
  C) глубокий режим, ответ обрезан по бюджету → продолжение (cont=1).
"""
import asyncio, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com",
                  LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID", "KIMI_API_KEY", "KIMI_BASE_URL",
          "KIMI_MODEL", "KIMI_MODEL_FAST", "KIMI_MODEL_DEEP", "KIMI_EFFORT",
          "KIMI_MAX_TOKENS", "KIMI_MAX_TOKENS_DEEP", "KIMI_MAX_TOKENS_CEILING", "KIMI_THINK_EXTRA"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.join(ROOT, "prod", "bot-mini-app"))
import kimi, assist
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

CALLS = []
SCENARIO = {"mode": "B"}

HTML = "<html><body>" + ("Тест по Евгению Онегину. " * 40) + "</body></html>"


async def sse(request):
    body = await request.json()
    CALLS.append(body)
    n = len(CALLS)
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8"})
    await resp.prepare(request)

    async def send(delta, finish=None):
        chunk = {"choices": [{"delta": delta}]}
        if finish:
            chunk["choices"][0]["finish_reason"] = finish
        await resp.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())

    sc = SCENARIO["mode"]
    if sc == "A":                       # быстрый режим: мышления нет, сразу контент
        for i in range(3):
            await send({"content": HTML[i * 200:(i + 1) * 200]})
        await send({}, finish="stop")
    elif sc == "B":                     # бюджет сгорел на размышлениях → повтор
        for i in range(5):
            await send({"reasoning_content": "думаю %d… " % i})
        if n == 1:
            await send({}, finish="length")     # 1-й заход: ни одного кусочка ответа
        else:
            for i in range(3):
                await send({"content": HTML[i * 200:(i + 1) * 200]})
            await send({}, finish="stop")
    else:                               # C: ответ обрезан → продолжение
        for i in range(3):
            await send({"reasoning_content": "думаю %d… " % i})
        for i in range(3):
            await send({"content": HTML[i * 200:(i + 1) * 200]})
        await send({}, finish="length" if n == 1 else "stop")
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


async def main():
    app = web.Application()
    app.router.add_post("/chat/completions", sse)
    async with TestClient(TestServer(app)) as c:
        kimi.BASE, kimi.API_KEY = str(c.server.make_url("/")).rstrip("/"), "k"
        kimi._ACCEPTED.clear()
        for sc, deep in (("A", False), ("B", True), ("C", True)):
            SCENARIO["mode"] = sc
            CALLS.clear()
            hist = [{"role": "user", "content": "Напиши тест по Евгению Онегину"}]
            thinks, texts, statuses = [], [], []
            async for kind, v in assist.chat_stream(hist, deep=deep, fmt="pdf"):
                if kind == "think":
                    thinks.append(v)
                elif kind == "text":
                    texts.append(v)
                elif kind == "status":
                    statuses.append(v)
            print(f"[{sc}] deep={deep} запросов к API: {len(CALLS)} | статусы: {statuses}")
            print(f"[{sc}] think-кусочков: {len(thinks)} | текст: {sum(len(t) for t in texts)} символов")
            print(f"[{sc}] thinking в запросах: {[cb.get('thinking') for cb in CALLS]}")
            print()

asyncio.run(main())
