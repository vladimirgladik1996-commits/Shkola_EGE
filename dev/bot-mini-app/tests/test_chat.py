"""Чат с ИИ в мини-аппе: проверка истории, маршрута /api/chat и ответов модели (сеть подменена)."""
import asyncio, os, sqlite3, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", AUTH_LOGIN="Admin", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID", "KIMI_API_KEY"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
import assist, bot, kimi
def ok(c): print("  ✓", c)

def bad(raw):
    try: assist.chat_history(raw)
    except ValueError: return True
    return False

def bad_fmt(v):
    try: assist.chat_format(v)
    except ValueError: return True
    return False

U = lambda t: {"role": "user", "text": t}
A = lambda t: {"role": "assistant", "text": t}

# --- история ---
assert bad(None) and bad([]) and bad("x") and bad([{"role": "system", "text": "x"}]) and bad([U(1)]); ok("мусор в истории отклоняется")
assert bad([A("привет")]) and bad([U("а"), A("б")]); ok("последняя реплика обязана быть от пользователя")
assert bad([U("x" * (assist.CHAT_MAX_CHARS + 1))]); ok("слишком длинная реплика отклоняется")
assert bad([U("x")] * (2 * assist.CHAT_MAX_MSGS + 1)); ok("слишком длинная история отклоняется")
h = assist.chat_history([A("старое"), U(" привет "), A("здравствуйте"), U("как дела")])
assert [m["role"] for m in h] == ["user", "assistant", "user"] and h[0]["content"] == "привет"; ok("ведущий ответ ИИ срезается, текст обрезается по краям")
assert len(assist.chat_history([U(str(i)) if i % 2 == 0 else A(str(i)) for i in range(30)] + [U("н")])) <= assist.CHAT_MAX_MSGS; ok("в модель уходят только последние реплики")

# --- ответ модели: стрим мышления и текста (от имени Вишенки) ---
SEEN = []
async def fake_stream(messages, model=None, deep=False, json_mode=False, images=None, thinking=True):
    SEEN.append((messages, deep, images))
    yield "think", "сначала подумаю"
    yield "text", "ответ"
real = kimi.ask_stream; kimi.ask_stream = fake_stream
async def collect(**kw):
    ev = []
    async for pair in assist.chat_stream(h, **kw):
        ev.append(pair)
    return ev
try:
    ev = asyncio.run(collect(deep=False))
    msgs, deep, _ = SEEN[-1]
    assert ev == [("status", "think"), ("think", "сначала подумаю"), ("status", "write"), ("text", "ответ"), ("done", "ответ")]; ok("стрим: статусы Вишенки, дельты мышления и текста, готовый ответ")
    assert msgs[0]["role"] == "system" and msgs[1:] == h and not deep and "коротко" in msgs[0]["content"]; ok("быстрый режим: системная инструкция + вся история")
    asyncio.run(collect(deep=True))
    assert SEEN[-1][1] is True and "подробный" in SEEN[-1][0][0]["content"]; ok("глубокий режим включает deep")
    # --- формат ответа и вложения ---
    assert assist.chat_format(None) == "text" and assist.chat_format("pdf") == "pdf" and bad_fmt("html"); ok("формат ответа: text/docx/pdf/image, иное отклоняется")
    ev = asyncio.run(collect(deep=False, images=["QQ=="], docs="--- a.txt ---\nтекст"))
    msgs, _, imgs = SEEN[-1]
    assert ev[0] == ("status", "read") and imgs == ["QQ=="]; ok("с вложениями Вишенка сначала «читает материалы»")
    assert "Материалы" in msgs[-1]["content"] and "a.txt" in msgs[-1]["content"] and msgs[1:-1] == h[:-1]; ok("текст вложений дописывается к последней реплике")
    asyncio.run(collect(fmt="pdf"))
    assert "заголовок" in SEEN[-1][0][0]["content"]; ok("для Word/PDF модель просят оформить документ")
    asyncio.run(collect(fmt="image"))
    assert "HTML" in SEEN[-1][0][0]["content"] and "инфографик" in SEEN[-1][0][0]["content"]; ok("для картинки модель пишет HTML-инфографику")
    for f in ("docx", "pdf", "image"):
        asyncio.run(collect(fmt=f))
        assert "ВОПРОС" in SEEN[-1][0][0]["content"]
    ok("для файловых форматов модель может переспросить — «ВОПРОС: …» вместо файла")
    assert assist.chat_question("ВОПРОС: Какой класс?") == "Какой класс?" and assist.chat_question("просто ответ") is None \
        and assist.chat_question("  вопрос: всё?  ") == "всё?"; ok("уточняющий вопрос («ВОПРОС: …») отделяется от готового ответа")
    # --- сбой модели ---
    async def bad_stream(*a, **kw):
        yield "text", "обро"
        yield "err", "net"
    kimi.ask_stream = bad_stream
    ev = asyncio.run(collect())
    assert ev[-1] == ("err", "net") and ("done", "обро") not in ev; ok("сбой стрима — честный err, а не падение")
finally:
    kimi.ask_stream = real

# --- маршрут и фронт ---
paths = {(r.method, res.canonical) for res in bot.make_app().router.resources() for r in res}
assert ("POST", "/api/chat") in paths; ok("POST /api/chat зарегистрирован")
html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
assert 'tab=="ch"?chat()' in html and 'apiStream("/api/chat"' in html and "function chat()" in html; ok("вкладка «Чат с ИИ» подключена к стриму /api/chat")
assert "CH_HINTS" not in html and 'class="aic-s"' not in html; ok("чипсов быстрых запросов нет")
assert 'id="aic-md"' in html and 'id="aic-mic"' in html and 'id="aic-m"' in html; ok("режим ответа и микрофон встроены в поле ввода")
assert all(x in html for x in ('id="aic-fb"', 'id="aic-ab"', 'id="aic-att"', 'format:fmt', 'files')); ok("формат ответа и вложения встроены в поле ввода")
assert all(x in html for x in ("function chStep(", "Чат с Вишенкой", 'class="aist"', 'class="aith"', 'class="aitx"', "💜 Вишенка")); ok("шаги мышления и работы показываются от имени Вишенки")
assert all(x in html for x in ('class="aim a st', "function chStepNew(", "function chThink(", "CH_STEP_MAX")); ok("шаги и мысли идут отдельными сообщениями в диалоге (новое под старым)")
assert all(x in html for x in ("конвертирует HTML в PDF", "читает материалы", "думает", "пишет ответ", "chReadLabel(")); ok("любая операция видна как сообщение Вишенки (думает / читает / конвертирует …)")
assert 'ev.t=="tg"' in html and "Ответ также отправлен в Telegram" in html; ok("фронт помечает ответ, доставленный и в Telegram")
assert 'Application(client_max_size=12 * 1024 * 1024' in open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read(); ok("лимит тела запроса вмещает вложения")

# --- готовый ответ уходит и в чат приложения, и в Telegram (+ шаги работы видны в стриме) ---
import time, hashlib, hmac, json as _json
from urllib.parse import urlencode
from aiohttp.test_utils import TestClient, TestServer
import auth
mem = sqlite3.connect(":memory:", check_same_thread=False); bot.db.backup(mem); mem.row_factory = sqlite3.Row
bot.db = mem; auth.init(mem)
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp())); mem.commit()
def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": _json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)
H = {"X-Init": init(42, "Gladik_N")}
class FakeBot:
    def __init__(self): self.msgs, self.photos, self.docs = [], [], []
    async def send_message(self, chat_id, text, **kw): self.msgs.append((chat_id, text))
    async def send_photo(self, chat_id, photo, caption=None, **kw): self.photos.append((chat_id, caption))
    async def send_document(self, chat_id, document, caption=None, **kw): self.docs.append((chat_id, caption))
async def tg_run():
    app = bot.make_app(); app["bot"] = FakeBot()
    async with TestClient(TestServer(app)) as c:
        async def fake_stream(messages, model=None, deep=False, json_mode=False, images=None, thinking=True):
            yield "think", "сначала подумаю\n\n"
            yield "think", "и ещё мысль"
            yield "text", "ответ"
        real = kimi.ask_stream; kimi.ask_stream = fake_stream; key = kimi.API_KEY; kimi.API_KEY = "test"
        try:
            r = await c.post("/api/chat", json={"messages": [U("привет")]}, headers=H)
            ev = [_json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
            assert [e["t"] for e in ev] == ["status", "think", "think", "status", "text", "done", "tg"], ev
            assert ev[0]["v"] == "think" and ev[3]["v"] == "write"; ok("стрим мышления Кими: статусы и дельты размышлений от имени Вишенки")
            assert ev[1]["v"] == "сначала подумаю\n\n" and ev[2]["v"] == "и ещё мысль"   # мысли — стримом, по абзацам
            assert ev[4]["v"] == "ответ" and ev[5]["reply"] == "ответ" and ev[5]["enabled"] is True
            assert app["bot"].msgs == [(42, "ответ")]; ok("ответ пришёл в чат приложения и одновременно в Telegram")
            assert ev[6] == {"t": "tg"}; ok("стрим подтверждает доставку ответа в Telegram")
            # формат-картинка: шаги конвертации видны в стриме, файл уходит боту
            async def html_stream(messages, model=None, deep=False, json_mode=False, images=None, thinking=True):
                yield "text", HTML
            kimi.ask_stream = html_stream
            r = await c.post("/api/chat", json={"messages": [U("инфографика")], "format": "image"}, headers=H)
            ev = [_json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
            kinds = [(e["t"], e.get("v")) for e in ev]
            assert ("status", "pdf") in kinds and ("status", "png") in kinds, kinds   # Вишенка конвертирует HTML в PDF и рисует инфографику
            done = [e for e in ev if e["t"] == "done"][0]
            assert done["file"]["kind"] == "image" and done["file"]["sent"] is True and done["file"]["img"].startswith("data:image/png;base64,")
            assert app["bot"].photos and kinds[-1] == ("tg", None); ok("картинка: шаги конвертации в стриме + файл ушёл в Telegram")
            # форматы Word и PDF: ответ модели собирается в файл и уходит боту документом
            async def doc_stream(messages, model=None, deep=False, json_mode=False, images=None, thinking=True):
                yield "text", "# Разбор\n- пункт"
            kimi.ask_stream = doc_stream
            r = await c.post("/api/chat", json={"messages": [U("разбор")], "format": "docx"}, headers=H)
            ev = [_json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
            done = [e for e in ev if e["t"] == "done"][0]
            assert done["file"]["kind"] == "docx" and done["file"]["sent"] is True and done["file"]["name"].endswith(".docx")
            assert app["bot"].docs; ok("Word: документ собран и ушёл в Telegram")
            kimi.ask_stream = html_stream
            r = await c.post("/api/chat", json={"messages": [U("отчёт")], "format": "pdf"}, headers=H)
            ev = [_json.loads(x) for x in (await r.text()).splitlines() if x.strip()]
            done = [e for e in ev if e["t"] == "done"][0]
            assert done["file"]["kind"] == "pdf" and done["file"]["mime"] == "application/pdf" and done["file"]["sent"] is True
            assert ("status", "file") in [(e["t"], e.get("v")) for e in ev]
            ok("PDF: HTML модели конвертировался в PDF, файл ушёл в Telegram")
        finally:
            kimi.ask_stream = real; kimi.API_KEY = key
HTML = ("<html><head><title>Тема</title><style>body{background-color:#FBF7E9;color:#0E2250}"
        "h1{font-size:24pt}div{background-color:#FFFFFF;border:1pt solid #E9E1C8;padding:10pt}</style></head>"
        "<body><h1>Тема недели</h1><div>Раз — и длинный текст карточки, чтобы проверить переносы строк и русский шрифт.</div>"
        "<div>Два</div></body></html>")
asyncio.run(tg_run())
print("test_chat: OK")
print("test_chat: OK")
