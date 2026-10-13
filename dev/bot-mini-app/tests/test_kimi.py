"""Клиенты API: kimi.py (Chat Completions: JSON-режим, vision, дешёвая/сильная модели) и
whisper.py (распознавание голоса). Сеть подменена фейковым сервером. Любая ошибка API —
None и запись в лог, бот не падает."""
import asyncio, json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOK = "123456:TESTTOKEN"
os.environ.update(BOT_TOKEN=TOK, AUTH_LOGIN="Admin", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID", "KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL",
          "WHISPER_API_KEY", "WHISPER_BASE_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import kimi, whisper
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
def ok(c): print("  ✓", c)

CALLS = []
MODE = {"code": 200, "json": {"choices": [{"message": {"content": "Привет!"}}]}}
STT = {"code": 200, "text": "добавь занятие в среду в двенадцать"}

async def fake_kimi(request):
    CALLS.append(("chat", dict(request.headers), await request.json()))
    return web.json_response(MODE["json"], status=MODE["code"])

async def fake_stt(request):
    CALLS.append(("stt", dict(request.headers), dict(await request.post())))
    return web.json_response({"text": STT["text"]}, status=STT["code"])

fk = web.Application()
fk.router.add_post("/chat/completions", fake_kimi)
fk.router.add_post("/audio/transcriptions", fake_stt)

async def main():
    async with TestClient(TestServer(fk)) as c:
        kimi.BASE = whisper.BASE = str(c.make_url("")).rstrip("/")

        # --- без ключей наружу ничего не уходит -----------------------------
        kimi.API_KEY = whisper.API_KEY = ""
        assert await kimi.ask([{"role": "user", "content": "привет"}]) is None
        assert await whisper.transcribe(b"zzz", "audio/ogg") is None
        assert not CALLS
        ok("без ключей запросы не уходят")

        # --- kimi: обычный запрос -------------------------------------------
        kimi.API_KEY = "TESTKEY"
        r = await kimi.ask([{"role": "user", "content": "привет"}])
        assert r == "Привет!"
        h, b = CALLS[-1][1], CALLS[-1][2]
        assert h["Authorization"] == "Bearer TESTKEY" and b["model"] == "kimi-k2.6"
        assert b["thinking"] == {"type": "disabled"} and b["max_tokens"] == 800
        assert "response_format" not in b and "reasoning_effort" not in b
        ok("обычный запрос: kimi-k2.6, thinking выключен, заголовок авторизации")

        # --- kimi: глубокий режим (kimi-k3 + reasoning_effort=max) ----------
        r = await kimi.ask([{"role": "user", "content": "сочинение"}], deep=True)
        b = CALLS[-1][2]
        assert r == "Привет!" and b["model"] == "kimi-k3" and b["reasoning_effort"] == "max"
        assert b["max_tokens"] == kimi.MAX_TOKENS_DEEP and "thinking" not in b
        ok("глубокий режим: kimi-k3, reasoning_effort=max, увеличенный лимит токенов")

        # --- kimi: JSON-режим и vision --------------------------------------
        MODE["json"] = {"choices": [{"message": {"content": json.dumps({"kind": "chat", "n": 1})}}]}
        j = await kimi.ask_json([{"role": "user", "content": "разбери"}])
        b = CALLS[-1][2]
        assert j == {"kind": "chat", "n": 1} and b["response_format"] == {"type": "json_object"}
        ok("JSON-режим: response_format json_object, ответ разбирается в dict")

        await kimi.ask([{"role": "user", "content": "что на фото"}], images=["QUJDRA=="])
        b = CALLS[-1][2]
        assert b["messages"][-1]["content"][0]["type"] == "text"
        assert b["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        ok("vision: картинка уходит content-массивом с image_url")

        # --- kimi: ошибки API и мусор ---------------------------------------
        MODE["code"] = 500
        assert await kimi.ask([{"role": "user", "content": "привет"}]) is None
        MODE["code"] = 200; MODE["json"] = {"foo": "нет choices"}
        assert await kimi.ask([{"role": "user", "content": "привет"}]) is None
        assert await kimi.ask_json([{"role": "user", "content": "привет"}]) is None
        MODE["json"] = {"choices": [{"message": {"content": "не json"}}]}
        assert await kimi.ask_json([{"role": "user", "content": "привет"}]) is None
        ok("500 / мусор вместо ответа / кривой JSON → None, без исключений")

        # --- whisper ---------------------------------------------------------
        MODE["json"] = {"choices": [{"message": {"content": "ok"}}]}
        whisper.API_KEY = "STTKEY"
        t = await whisper.transcribe(b"\x00\x01voice-bytes", "audio/ogg")
        assert t == "добавь занятие в среду в двенадцать"
        h, form = CALLS[-1][1], CALLS[-1][2]
        assert h["Authorization"] == "Bearer STTKEY" and form["model"] == "whisper-1" and form["language"] == "ru"
        assert form["file"].filename == "voice.ogg"
        ok("whisper: ogg-запись уходит multipart-запросом, текст возвращается")

        t = await whisper.transcribe(b"x", "audio/webm")
        assert CALLS[-1][2]["file"].filename == "voice.webm"
        STT["code"] = 500
        assert await whisper.transcribe(b"x", "audio/webm") is None
        ok("webm тоже принимается; 500 от STT → None")
asyncio.run(main())
print("OK")
