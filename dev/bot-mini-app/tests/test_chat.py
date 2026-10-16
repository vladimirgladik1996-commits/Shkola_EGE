"""Чат с ИИ в мини-аппе: проверка истории, маршрута /api/chat и ответов модели (сеть подменена)."""
import asyncio, os, sys
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

# --- ответ модели ---
SEEN = []
async def fake_ask(messages, model=None, deep=False, json_mode=False, images=None):
    SEEN.append((messages, deep)); return "ответ"
real = kimi.ask; kimi.ask = fake_ask
try:
    r = asyncio.run(assist.chat_reply(h, deep=False))
    msgs, deep = SEEN[-1]
    assert r == "ответ" and msgs[0]["role"] == "system" and msgs[1:] == h and not deep and "коротко" in msgs[0]["content"]; ok("быстрый режим: системная инструкция + вся история")
    asyncio.run(assist.chat_reply(h, deep=True))
    assert SEEN[-1][1] is True and "подробный" in SEEN[-1][0][0]["content"]; ok("глубокий режим включает deep")
finally:
    kimi.ask = real

# --- маршрут и фронт ---
paths = {(r.method, res.canonical) for res in bot.make_app().router.resources() for r in res}
assert ("POST", "/api/chat") in paths; ok("POST /api/chat зарегистрирован")
html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
assert 'tab=="ch"?chat()' in html and 'api("/api/chat"' in html and "function chat()" in html; ok("вкладка «Чат с ИИ» подключена к /api/chat")
assert "CH_HINTS" not in html and 'class="aic-s"' not in html; ok("чипсов быстрых запросов нет")
assert 'id="aic-md"' in html and 'id="aic-mic"' in html and 'id="aic-m"' in html; ok("режим ответа и микрофон встроены в поле ввода")
print("test_chat: OK")
