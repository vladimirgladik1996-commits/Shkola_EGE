"""Быстрый вход в APK через Telegram: заявка → подтверждение в боте → токен приложению (без пароля).
Проверяется серверная логика (auth.py), обработчики HTTP (на заглушке aiohttp, как в test_auth.py)
и «проводка» между ботом, страницей, манифестом и MainActivity в обеих копиях (dev и prod)."""
import asyncio, json, os, re, sqlite3, sys, time, types
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(ROOT))
os.environ.update(PUBLIC_URL="https://bot.example.com", BOT_USERNAME="@Test_Bot")
os.environ.pop("APP_ENV", None); os.environ.pop("APK_SCHEME", None)
sys.path.insert(0, ROOT)

class Resp:
    def __init__(s, status=200, headers=None, text=None, body=None): s.status, s.headers, s.text, s.body = status, dict(headers or {}), text, body
web = types.SimpleNamespace(
    Response=lambda text=None, content_type=None, charset=None, headers=None, status=200: Resp(status, headers, text),
    json_response=lambda data, status=200, headers=None: Resp(status, headers, None, data))
sys.modules["aiohttp"] = types.SimpleNamespace(web=web)
class Router:
    def __init__(s): s.r = {}
    def add_get(s, p, h): s.r[("GET", p)] = h
    def add_post(s, p, h): s.r[("POST", p)] = h
class App: router = Router()
class Req:
    def __init__(s, body=b"{}", origin="https://bot.example.com", ctype="application/json"):
        s.headers = {"Origin": origin} if origin is not None else {}
        s.content_type, s._b, s.content_length = ctype, body, len(body)
    async def read(s): return s._b

import auth
auth.ALLOWED_IDS |= {7}
auth.PW_HASH = auth.make_hash("Alfa1786")
db = sqlite3.connect(":memory:"); auth.init(db)
app = App(); auth.setup(app)
START, POLL, BACK = app.router.r[("POST", "/api/tglogin/start")], app.router.r[("POST", "/api/tglogin/poll")], app.router.r[("GET", "/app-return")]
def ok(c): print("  ✓", c)
run = asyncio.run
pollreq = lambda p: Req(json.dumps({"poll": p}).encode())

async def flow():
    assert auth.BOT_USERNAME == "Test_Bot"
    # --- старт: ссылка на бота + секрет опроса, код и секрет разные ---
    r = await START(Req()); j = r.body
    assert r.status == 200 and j["ok"] and j["tg"].startswith("tg://resolve?domain=Test_Bot&start=login_") and j["url"].startswith("https://t.me/Test_Bot?start=login_"), j
    code = j["tg"].split("login_")[1]; poll = j["poll"]
    assert code != poll and code not in json.dumps({k: v for k, v in j.items() if k in ("poll",)})
    assert db.execute("select count(*) from auth_tg").fetchone()[0] == 1
    assert code not in str(db.execute("select * from auth_tg").fetchall()), "в БД только хэши"
    ok("start: ссылка t.me/tg:// с кодом, секрет опроса отдельно, в БД только хэши")

    # --- до подтверждения — ждём ---
    r = await POLL(pollreq(poll)); assert r.status == 200 and r.body["status"] == "pending" and not r.body["ok"] and "token" not in r.body
    ok("poll до подтверждения: pending, токена нет")

    # --- код без права доступа и мусор не подтверждают ---
    assert not auth.tg_confirm(code, 999, "stranger_x"), "чужой Telegram-ID"
    assert not auth.tg_confirm("short", 7) and not auth.tg_confirm(None, 7) and not auth.tg_confirm("x" * 30, 7)
    assert (await POLL(pollreq(poll))).body["status"] == "pending"
    ok("не из белого списка / битый код — подтвердить нельзя")

    # --- подтверждение из белого списка → токен ровно один раз ---
    assert auth.tg_confirm(code, 7, "")
    assert not auth.tg_confirm(code, 7, ""), "повторное подтверждение"
    r = await POLL(pollreq(poll)); tok = r.body.get("token")
    assert r.status == 200 and r.body["ok"] and tok and auth.check_token(tok) == 7, r.body
    # ответ мог потеряться (страница перезагрузилась) — в течение TG_GRACE тот же держатель секрета получает новый токен
    r = await POLL(pollreq(poll)); tok2 = r.body.get("token")
    assert r.status == 200 and tok2 and tok2 != tok and auth.check_token(tok2) == 7
    assert not auth.tg_confirm(code, 7, ""), "заявку подтвердить заново нельзя"
    assert db.execute("select exp-? from auth_tg", (time.time(),)).fetchone()[0] <= auth.TG_GRACE + 1
    db.execute("update auth_tg set exp=?", (time.time() - 1,)); db.commit()
    r = await POLL(pollreq(poll)); assert r.status == 410 and r.body["status"] == "gone" and "token" not in r.body
    ok("токен работает как токен входа; потерянный ответ — повтор в течение 30 с; потом заявка закрыта (410)")

    # --- чужой, чужой poll, истёкшая заявка ---
    for bad in (None, "", "x", "a" * 70, "'; drop table auth_tg;--"):
        r = await POLL(pollreq(bad)); assert r.status == 410, (bad, r.status)
    c2, p2 = auth.tg_start()
    db.execute("update auth_tg set exp=?", (time.time() - 1,)); db.commit()
    assert (await POLL(pollreq(p2))).status == 410 and not auth.tg_confirm(c2, 7)
    assert db.execute("select count(*) from auth_tg").fetchone()[0] == 0
    ok("мусорный/неизвестный/истёкший секрет → 410; истёкшую заявку подтвердить нельзя")

    # --- отмена и исключение из списка за время ожидания ---
    c3, p3 = auth.tg_start(); auth.tg_cancel(c3)
    assert (await POLL(pollreq(p3))).status == 410
    c4, p4 = auth.tg_start(); assert auth.tg_confirm(c4, 7)
    auth.ALLOWED_IDS.discard(7)
    try:
        assert (await POLL(pollreq(p4))).status == 410
    finally:
        auth.ALLOWED_IDS.add(7)
    ok("отмена в боте и исключение из списка до получения токена → токен не выдаётся")

    # --- CSRF: чужой Origin и не-JSON ---
    assert (await START(Req(origin="https://evil.example"))).status == 403 and (await START(Req(origin=None))).status == 403
    assert (await POLL(Req(json.dumps({"poll": poll}).encode(), origin="https://evil.example"))).status == 403
    assert (await POLL(Req(b"x", ctype="text/plain"))).status == 403
    ok("чужой Origin / не JSON → 403")

    # --- лимит очереди ---
    for _ in range(auth.TG_MAX_PENDING): assert auth.tg_start()
    r = await START(Req()); assert r.status == 429, r.status
    db.execute("delete from auth_tg"); db.commit()
    ok("очередь заявок ограничена (429)")

    # --- имя и аватар из Telegram — в панель приложения (задача 5: вход через Telegram в АПК) ---
    c5, p5 = auth.tg_start()
    assert auth.tg_confirm(c5, 7, "", "  Иван\x00 Петров ", "data:image/jpeg;base64,QUJD")
    j = (await POLL(pollreq(p5))).body
    assert j["ok"] and j["token"] and j["name"] == "Иван Петров" and j["photo"] == "data:image/jpeg;base64,QUJD", j
    c6, p6 = auth.tg_start()
    assert auth.tg_confirm(c6, 7, "", "X" * 200, "https://evil.example/a.jpg")   # имя длиннее 60, аватар-ссылка — мусор
    j = (await POLL(pollreq(p6))).body
    assert j["name"] == "X" * 60 and "photo" not in j, j
    ok("имя и аватар из Telegram доходят до приложения; мусорный аватар и имя длиннее 60 отсекаются")

    # --- страница возврата в APK ---
    r = await BACK(Req()); assert r.status == 200 and "kabinet://login" in r.text and "kabinet-dev" not in r.text
    assert "script-src 'nonce-" in r.headers["Content-Security-Policy"]
    ok("/app-return открывает kabinet://login (схема prod), CSP с nonce")
    assert "intent://login#Intent;scheme=kabinet;package=ru.kabinet.tutor;end" in r.text and "ru.kabinet.tutor.dev" not in r.text
    assert 'id="a" href="kabinet://login"' in r.text and "__P__" not in r.text and "__S__" not in r.text and "hidden" in r.text
    ok("/app-return: на Android — intent://-ссылка с пакетом prod, запасная kabinet://, подсказка «не открылось»")

run(flow())

# --- без имени бота ссылку выдать нельзя ---
auth.BOT_USERNAME = ""
assert run(START(Req())).status == 403
auth.BOT_USERNAME = "Test_Bot"
ok("имя бота не определено → заявку не создаём")

# --- проводка dev/prod ---
rd = lambda *p: open(os.path.join(REPO, *p), encoding="utf-8").read()
for side, scheme in (("dev", "kabinet-dev"), ("prod", "kabinet")):
    man = rd(side, "apk", "AndroidManifest.xml"); java = rd(side, "apk", "src", "ru", "kabinet", "tutor", "MainActivity.java")
    html = rd(side, "bot-mini-app", "docs", "index.html"); bot = rd(side, "bot-mini-app", "bot.py"); a = rd(side, "bot-mini-app", "auth.py")
    assert f'android:scheme="{scheme}"' in man and 'android:host="login"' in man and 'android:launchMode="singleTask"' in man, side
    assert 'android.intent.category.BROWSABLE' in man, side
    assert "onNewIntent" in java and "__tgPoll" in java, side
    assert '"/api/tglogin/start"' in a.replace("'", '"') or "/api/tglogin/start" in a
    assert "/api/tglogin/start" in html and "/api/tglogin/poll" in html and "window.__tgPoll" in html and "localStorage.tgLogin" in html, side
    assert 'startswith("login_")' in bot and 'F.data.startswith("tgl:")' in bot and "auth.tg_confirm" in bot and "/app-return" in bot, side
    assert "get_me()" in bot, side
    assert "localStorage.tgLogin&&document.visibilityState" in html, f"{side}: wake не должен перезагружать страницу во время входа"
    assert 'auth.BASE.startswith("https://")' in bot, side
    assert "localStorage.sesName=j.name" in html and "localStorage.sesPhoto=j.photo" in html, side
    assert '$("#drawer .mn-foot").hidden' in html and "Скачать АПК" in html, side
    assert "def tg_profile(" in bot and "get_user_profile_photos" in bot and "name, photo)" in bot, side
    assert "alter table auth_tg add column photo" in a and "_tg_profile" in a, side
    ok(f"{side}: панель АПК — имя/аватар из Telegram при входе, «Скачать АПК» в приложении скрыт")
    assert "tgl:y:" not in bot and "auth.logout(c.from_user.id)" in bot, side   # вход без вопросов + кнопка отзыва
    ok(f"{side}: манифест ({scheme}://login, singleTask), MainActivity, страница и бот связаны")
print("OK")
