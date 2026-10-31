"""Стрим мышления не должен замирать: «Вишенка думает» стримится в ЛЮБОМ заходе модели.

Баг-репорт 016: пользователь дал команду «напиши тест по Евгению Онегину» и выбрал ответ
в PDF — «стриминг мышления» не работал. Две причины, обе закрыты:

  1) kimi.ask_stream отдавал reasoning_content только в первом заходе
     (`if th and attempt == 0 and cont == 0`). Ровно в тяжёлых сценариях (тест в PDF:
     ответ — огромный HTML-документ) первый заход почти всегда обрезается
     (finish_reason=length — бюджет max_tokens сгорает на размышлениях или на тексте),
     и всё видимое мышление шло в повтор/продолжение — а там уже скрывалось.
     Пользователь видел замерший «Вишенка думает» без единого нового символа.
  2) Мини-апп позволял выбрать PDF в режиме «Быстрый ответ», а мышление включено только
     в «Долгом» (assist.chat_stream: thinking=bool(deep)). В боте формат выбирается только
     после «Долгого ответа» («Быстрый всегда текстом») — теперь и мини-апп так же:
     файловый формат сам включает долгий конвейер.

Тест держит оба исправления на месте. Запуск: python3 tests/test_think_stream.py
"""
import asyncio, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com",
                  LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID", "KIMI_API_KEY", "KIMI_BASE_URL",
          "KIMI_MODEL", "KIMI_MODEL_FAST", "KIMI_MODEL_DEEP", "KIMI_EFFORT",
          "KIMI_MAX_TOKENS", "KIMI_MAX_TOKENS_DEEP", "KIMI_MAX_TOKENS_CEILING", "KIMI_THINK_EXTRA"):
    os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import assist, kimi
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def ok(m):
    print("  ✓", m)


RESPONSES = []      # ([дельт…], finish_reason|None, done?) — по одному на каждый запрос


async def completions(request):
    await request.json()
    deltas, finish, done = RESPONSES.pop(0)
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8"})
    await resp.prepare(request)
    for i, d in enumerate(deltas):
        chunk = {"choices": [{"delta": d}]}
        if i == len(deltas) - 1 and finish:
            chunk["choices"][0]["finish_reason"] = finish
        await resp.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
    if done:
        await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


async def stream(messages, **kw):
    return [item async for item in kimi.ask_stream(messages, **kw)]


async def main():
    app = web.Application()
    app.router.add_post("/chat/completions", completions)
    async with TestClient(TestServer(app)) as c:
        kimi.BASE = str(c.make_url("")).rstrip("/")
        kimi.API_KEY = "***"
        kimi._ACCEPTED.clear()

        # --- 1) бюджет сгорел на размышлениях → ПОВТОР: мышление стримится в обоих заходах ---
        RESPONSES.append(([{"reasoning_content": "сначала подумаю… "}], "length", True))
        RESPONSES.append(([{"reasoning_content": "во второй раз думаю… "},
                           {"content": "Ответ готов."}], "stop", True))
        out = await stream([{"role": "user", "content": "сделай тест"}], deep=True, thinking=True)
        thinks = [v for k, v in out if k == "think"]
        text = "".join(v for k, v in out if k == "text")
        assert thinks == ["сначала подумаю… ", "во второй раз думаю… "], out
        assert text == "Ответ готов." and not [k for k, _ in out if k == "err"], out
        ok("повтор после пустого стрима: мышление стримится в обоих заходах (не замирает)")

        # --- 2) обрезанный ответ → ПРОДОЛЖЕНИЕ: мышление видно и в начале, и при продолжении ---
        RESPONSES.append(([{"reasoning_content": "планирую тест… "},
                           {"content": "Вопрос 1. "}], "length", True))
        RESPONSES.append(([{"reasoning_content": "дописываю ответы… "},
                           {"content": "Ответы: 1) а"}], "stop", True))
        out = await stream([{"role": "user", "content": "сделай тест"}], deep=True, thinking=True)
        assert [v for k, v in out if k == "think"] == ["планирую тест… ", "дописываю ответы… "], out
        assert "".join(v for k, v in out if k == "text") == "Вопрос 1. Ответы: 1) а", out
        ok("продолжение обрезанного ответа: мышление тоже стримится")

    # --- 3) файловый формат (PDF/Word/картинка) — всегда долгий конвейер с мышлением ---
    assert 'deep = bool(deep) or fmt != "text"' in open(
        os.path.join(ROOT, "assist.py"), encoding="utf-8").read(), \
        "assist.chat_stream должен включать deep для файловых форматов (мышление + сильная модель)"
    ok("assist.chat_stream: файловый формат включает долгий режим (мышление не выключено)")

    SEEN = []
    async def fake_stream(messages, model=None, deep=False, json_mode=False, images=None,
                          thinking=False, max_tokens=None):
        SEEN.append({"deep": deep, "thinking": thinking})
        yield "text", "ответ"
    real = kimi.ask_stream
    kimi.ask_stream = fake_stream
    try:
        async def collect(**kw):
            return [pair async for pair in assist.chat_stream([{"role": "user", "content": "напиши тест"}], **kw)]
        await collect(deep=False, fmt="pdf")
        assert SEEN[-1]["deep"] is True and SEEN[-1]["thinking"] is True, SEEN[-1]
        await collect(deep=False, fmt="docx")
        assert SEEN[-1]["deep"] is True and SEEN[-1]["thinking"] is True, SEEN[-1]
        await collect(deep=False, fmt="image")
        assert SEEN[-1]["deep"] is True and SEEN[-1]["thinking"] is True, SEEN[-1]
        await collect(deep=False, fmt="text")
        assert SEEN[-1]["deep"] is False and SEEN[-1]["thinking"] is False, SEEN[-1]
    finally:
        kimi.ask_stream = real
    ok("быстрый режим + PDF/Word/картинка: модель зовётся с deep и мышлением; текст — как просили")

    # --- 4) интерфейс: файловый формат сам переключает «Долгий ответ», «Быстрый» сбрасывает формат ---
    html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
    assert re.search(r"function chFmtSet\(k\)\{chFmt=k;if\(k\)\{chDeep=true;.*?chMenu\(\)\}", html), \
        "chFmtSet: выбор файла (Word/PDF/картинка) должен включать «Долгий ответ»"
    assert re.search(r"function chMode\(d\)\{chDeep=!!d;if\(!d\)\{chFmt=0;.*?chMenu\(\)\}", html), \
        "chMode: «Быстрый ответ» должен сбрасывать формат на «Текст»"
    ok("мини-апп: файл включает «Долгий ответ», «Быстрый ответ» всегда текстом — как в боте")

    print("test_think_stream: OK")


asyncio.run(main())
