"""Keep-alive: пинг собственного публичного URL каждые N секунд (анти-сон Render free plan).
Сеть Telegram не используется; поднимается локальный HTTP-сервер-счётчик запросов."""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", AUTH_LOGIN="Admin", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import bot
from aiohttp import web

def ok(c): print("  ✓", c)

async def main():
    # 1. интервал по умолчанию — 3 минуты
    assert bot.KA_SEC == 180, bot.KA_SEC
    ok("интервал по умолчанию 180 с (3 минуты), меняется KEEPALIVE_SEC")

    # 2. пинг реально ходит по WEBAPP_URL с нужным интервалом
    hits = []
    async def h(request):
        hits.append(request.path); return web.Response(text="ok")
    app = web.Application(); app.router.add_get("/", h)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    port = runner.addresses[0][1]

    bot.URL = f"http://127.0.0.1:{port}"
    bot.KA_SEC = 1
    t = asyncio.create_task(bot.keepalive())
    await asyncio.sleep(2.6)
    n = len(hits)
    assert n >= 2, f"слишком мало пингов: {hits}"
    ok(f"keepalive сделал {n} запросов за ~2.6 с на {bot.URL}/ (интервал ~1 с соблюдается)")

    # 3. недоступный адрес не роняет задачу — цикл продолжает работать
    await runner.cleanup()
    await asyncio.sleep(1.5)
    assert not t.done(), f"задача умерла при недоступном URL: {t.exception() if t.done() else ''}"
    t.cancel()
    try: await t
    except asyncio.CancelledError: pass
    ok("недоступный URL не роняет задачу: ошибка логируется, цикл продолжается")

    # 4. без WEBAPP_URL — тихо выключен и не падает
    bot.URL = ""
    await bot.keepalive()
    ok("без WEBAPP_URL keepalive выключен и не падает")

asyncio.run(main())
print("test_keepalive: OK")
