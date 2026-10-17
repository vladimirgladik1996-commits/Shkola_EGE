"""Чат с ИИ × материалы × файлы:
  • модель получает материалы раздела «Материалы» ДО любого поиска в интернете — по найденному
    варианту подставляются тест (задания) и текущее сочинение/эссе (см. matlib + assist.chat_stream);
  • файлы ответа (docx/pdf/png) скачиваются прямо из чата по подписной ссылке и пересылаются:
    чип файла кликабельный, в APK — системное окно пересылки, документ ботом — в диалог;
  • виджет Вишенки открывает страницу материала (open) и уточняет каталог (choose).
Сеть Telegram и модель подменены. Запуск: python3 tests/test_chat_materials.py
"""
import asyncio, hashlib, hmac, json, os, sqlite3, sys, time
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL",
                  KIMI_API_KEY="123456:TESTKEY")   # ключ нужен, чтобы /api/chat не отвечал заглушкой «off»; модель подменена ниже
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import assist, auth, bot, kimi, matlib
from aiohttp.test_utils import TestClient, TestServer


def ok(c):
    print("  ✓", c)


# --- 1. модель получает материалов раздела «Материалы» до интернета ---
SEEN = []


async def fake_stream(messages, model=None, deep=False, json_mode=False, images=None, thinking=True):
    SEEN.append(messages)
    yield "text", "Готово: переписал эссе по варианту."


async def collect(hist, mat):
    out = []
    async for pair in assist.chat_stream(hist, mat=mat):
        out.append(pair)
    return out


real_stream = kimi.ask_stream
kimi.ask_stream = fake_stream
try:
    mat_ctx, _ = matlib.context("перепиши эссе к варианту ОГЭ 4 из сборника дощинского")
    ev = asyncio.run(collect([{"role": "user", "content": "перепиши эссе к варианту ОГЭ 4 из сборника дощинского"}], mat_ctx))
    plain = asyncio.run(collect([{"role": "user", "content": "какая погода"}], ""))
finally:
    kimi.ask_stream = real_stream

msgs = SEEN[0]
assert "МАТЕРИАЛЫ ИЗ РАЗДЕЛА" in msgs[-1]["content"] and "Исходный текст" in msgs[-1]["content"]
assert msgs[-1]["content"].index("МАТЕРИАЛЫ ИЗ РАЗДЕЛА") > msgs[-1]["content"].index("перепиши эссе")
assert "ПОРЯДОК РАБОТЫ С ИСТОЧНИКАМИ" in msgs[0]["content"] and "интернет" in msgs[0]["content"]
assert ev[-1] == ("done", "Готово: переписал эссе по варианту.")
ok("в запрос модели подставлены материалы каталога (тест варианта и текущее сочинение) — до интернета")
assert "МАТЕРИАЛЫ ИЗ РАЗДЕЛА" not in SEEN[1][-1]["content"] and plain[-1][0] == "done"
ok("без совпадений в материалах модель отвечает без них (проверка материалов выполняется всегда)")

# --- 2. HTTP: файл ответа доступен по подписной ссылке из чата (в любом формате) ---
mem = sqlite3.connect(":memory:", check_same_thread=False)
bot.db.backup(mem)
mem.row_factory = sqlite3.Row
bot.db = mem
auth.init(mem)
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


async def main():
    async with TestClient(TestServer(bot.make_app())) as c:
        # файл кладётся в память и отдаётся по ?tok= БЕЗ заголовков входа (нативные загрузчики идут так)
        bot.chat_file_keep("AbCdEfGh12", b"PDFDATA", "Ответ.pdf", "application/pdf", 42)
        tok = bot.file_token(42, "AbCdEfGh12")
        r = await c.get("/api/chatfile/AbCdEfGh12?tok=" + tok)
        assert r.status == 200 and await r.read() == b"PDFDATA"
        assert r.headers["Content-Type"] == "application/pdf" and "attachment" in r.headers["Content-Disposition"]
        ok("файл чата скачивается прямо из чата по подписной ссылке (без заголовков входа)")
        assert (await c.get("/api/chatfile/AbCdEfGh12")).status == 403          # без подписи — нельзя
        assert (await c.get("/api/chatfile/AbCdEfGh12?tok=" + bot.file_token(7, "AbCdEfGh12"))).status == 403
        assert (await c.get("/api/chatfile/NoSuchFile1?tok=" + bot.file_token(42, "NoSuchFile1"))).status == 404
        ok("чужая ссылка и битый токен не проходят: файл доступен только своему владельцу")

        # /api/chat: модель отвечает, формат docx → в событии done есть file.url для кликабельного чипа
        async def fake_chat_stream(history, deep=False, images=None, docs=None, fmt="text", mat=""):
            assert "МАТЕРИАЛЫ ИЗ РАЗДЕЛА" in mat or "ничего не нашлось" in mat   # материалы проверяются всегда
            yield "text", "Эссе"
            yield "done", "# Эссе по варианту 4\n\nПереписанный текст."

        real_chat_stream = assist.chat_stream
        assist.chat_stream = fake_chat_stream
        try:
            r = await c.post("/api/chat", json={"messages": [{"role": "user", "text": "перепиши эссе к варианту ОГЭ 4"}],
                                                "deep": False, "format": "docx", "files": []}, headers=H)
            body = await r.text()
            assert r.status == 200, (r.status, body)
            evs = [json.loads(ln) for ln in body.splitlines() if ln.strip()]
        finally:
            assist.chat_stream = real_chat_stream
        done = [e for e in evs if e.get("t") == "done"][-1]
        f = done["file"]
        assert f["name"].endswith(".docx") and f["kind"] == "docx" and f["url"].startswith("/api/chatfile/")
        assert "wordprocessingml" in f["mime"]
        r2 = await c.get(f["url"])                                              # ссылка из чата — без заголовков
        assert r2.status == 200 and (await r2.read())[:2] == b"PK"
        ok("docx/pdf/png: файл в чате кликабельный — скачивается из чата и пересылается (mime верный)")
        assert done["reply"].startswith("Готово")
        ok("ответ чата не «падает», если бот не смог прислать документ в диалог — файл доступен из чата")

        # --- 3. виджет Вишенки: открыть материал и уточнить каталог ---
        r = await c.post("/api/voice", json={"ctx": {"tab": "mt"}, "text": "открой вариант 7 ЕГЭ"}, headers=H)
        d = await r.json()
        assert d["stage"] == "open" and d["open"] == {"cat": "ege-essays-2027", "n": 7}, d
        ok("виджет открывает страницу варианта на экране (open → мини-апп переходит в «Материалы»)")

        r = await c.post("/api/voice", json={"ctx": {"tab": "mt"}, "text": "найди эссе ОГЭ к варианту 4"}, headers=H)
        d = await r.json()
        assert d["stage"] == "clarify" and "какой" in d["reply"].lower(), d
        r = await c.post("/api/voice", json={"ctx": {"tab": "mt"}, "text": "до 80 слов"}, headers=H)
        d = await r.json()
        assert d["stage"] == "open" and d["open"] == {"cat": "oge-essays-2027-vishenka", "n": 4}, d
        ok("два сборника → виджет уточняет, какой нужен, и открывает по ответу")

        r = await c.post("/api/voice", json={"ctx": {"tab": "mt", "mat": {"cat": "oge-essays-2027", "n": 4, "part": "other"}},
                                             "text": "кто автор этого варианта"}, headers=H)
        d = await r.json()
        assert d["stage"] == "answer" and d["reply"], d
        ok("контекст открытой страницы (mat) доходит до виджета — он понимает, с каким вариантом работает")


asyncio.run(main())

# --- 4. фронт: чип файла кликабельный, Вишенка открывает материалы, контекст страницы виден ---
html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
assert "chFileTap" in html and "shareFile" in html and "tg.openLink" in html
assert 'x.u?`<a class="aif"' in html
ok("фронт: файл в чате — кликабельная ссылка (скачать) + пересылка (APK: shareFile, Telegram: openLink)")
assert "voiceOpen(" in html and "r.open" in html and "matOpen(ci,o.n)" in html
assert "c.mat={cat:matCur.cat" in html and 'matCur=typeof part=="number"' in html
ok("фронт: Вишенка открывает найденный вариант и видит, какой материал сейчас на экране")
assert "file={n:ev.file.name" in html and "ev.file.url" in html
ok("фронт: url и mime файла из стрима сохраняются в сообщении (чип работает и после перезапуска)")

print("test_chat_materials: OK")
