import os, sys, types, json, time, sqlite3, asyncio, hashlib, hmac
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(AUTH_LOGIN="Admin", PUBLIC_URL="https://Bot.Example.com:443/some/path")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- минимальная заглушка aiohttp.web (тестируем СВОЮ обвязку, не aiohttp) ---
class Resp:
    def __init__(s, status=200, headers=None, text=None, body=None):
        s.status, s.headers, s.text, s.body = status, dict(headers or {}), text, body
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
    def __init__(s, body=b"", origin="https://bot.example.com", ctype="application/json", fetch_site=None, clen="auto"):
        s.headers = {"Origin": origin} if origin is not None else {}
        if fetch_site: s.headers["Sec-Fetch-Site"] = fetch_site
        s.content_type, s._b = ctype, body
        s.content_length = len(body) if clen == "auto" else clen
    async def read(s): return s._b

import auth
assert auth.BASE == "https://bot.example.com", auth.BASE       # путь и порт 443 отброшены, регистр нормализован
auth.ALLOWED_IDS |= {1, 2, 3, 4}          # тестовые пользователи (проверка по нику — в test_access.py)
auth.PW_HASH = auth.make_hash("Alfa1786")
assert auth.PW_HASH.startswith("scrypt:65536:8:2:") and "$" not in auth.PW_HASH
db = sqlite3.connect(":memory:"); auth.init(db)
alerts, logins = [], []
async def on_alert(i, t): alerts.append((i, t))
async def on_login(i): logins.append(i)
app = App(); auth.setup(app, on_login, on_alert)
LOGIN_H, PAGE_H = app.router.r[("POST", "/api/login")], app.router.r[("GET", "/login")]
body = lambda t, l="Admin", p="Alfa1786": json.dumps({"t": t, "login": l, "password": p}).encode()
tok = lambda uid: auth.new_link(uid).split("#")[1]
def ok(c): print("  ✓", c)

async def main():
    # 1. страница: CSP с nonce, запрет фреймов, no-store, HSTS
    p = await PAGE_H(Req())
    assert "nonce-" in p.headers["Content-Security-Policy"] and "form-action 'none'" in p.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in p.headers["Content-Security-Policy"] and p.headers["X-Frame-Options"] == "DENY"
    assert p.headers["Cache-Control"] == "no-store" and "Strict-Transport-Security" in p.headers
    assert "__N__" not in p.text; ok("страница: CSP/nonce/no-store/HSTS/frame-deny")

    # 2. CSRF и подмена источника
    t = tok(1)
    for kw in (dict(origin="https://evil.com"), dict(origin=None), dict(origin="http://bot.example.com"),
               dict(fetch_site="cross-site"), dict(fetch_site="same-site")):
        assert (await LOGIN_H(Req(body(t), **kw))).status == 403, kw
    assert (await LOGIN_H(Req(body(t), ctype="text/plain"))).status == 400
    assert (await LOGIN_H(Req(body(t), clen=None))).status == 400
    assert (await LOGIN_H(Req(b"x" * 5000))).status == 400
    assert not auth.is_authorized(1); ok("CSRF/Origin/Sec-Fetch-Site/Content-Type/размер → отказ, попытки не потрачены")
    assert db.execute("select count(*) from auth_fails").fetchone()[0] == 0

    # 3. мусорные тела: ни одно не доходит до проверки пароля и не даёт 500
    bad = [b"[" * 4000, b"{", b"null", b"[]", b'{"t":1,"login":"a","password":"b"}', b'{"t":"short","login":"a","password":"b"}',
           json.dumps({"t": t, "login": "a" * 65, "password": "b"}).encode(), json.dumps({"t": t, "login": "a", "password": "b" * 129}).encode(),
           json.dumps({"t": t, "login": "a\x00", "password": "b"}).encode(), b'{"t":"' + t.encode() + b'","login":"\\ud800","password":"x"}',
           b"\xff\xfe\xfd", json.dumps({"t": t, "login": ["Admin"], "password": {"$ne": ""}}).encode(),
           json.dumps({"t": t, "login": {"$gt": ""}, "password": {"$gt": ""}}).encode()]
    for b in bad:
        r = await LOGIN_H(Req(b)); assert r.status == 400, (b[:40], r.status)
    assert db.execute("select count(*) from auth_fails").fetchone()[0] == 0; ok(f"{len(bad)} мусорных/типовых-инъекционных тел → 400 без траты попыток")

    # 4. SQL-инъекции в логине/пароле/токене: вход закрыт, таблицы целы
    for l, pw in (("Admin' OR '1'='1", "x' OR '1'='1"), ("'; drop table auth_sessions;--", "a"), ("Admin", "Alfa1786 ")):
        assert (await LOGIN_H(Req(body(t, l, pw)))).status == 401
    assert (await LOGIN_H(Req(body("A' OR '1'='1" + "A" * 20)))).status == 400    # токен вне формата
    assert db.execute("select count(*) from auth_sessions").fetchone()[0] == 0; ok("SQL-инъекции: 401/400, таблицы на месте")

    # 5. блокировка, ссылка сгорает, новая ссылка не обнуляет, владелец получает алерт
    for pw in ("w1", "w2"): await LOGIN_H(Req(body(t, "Admin", pw)))
    assert (await LOGIN_H(Req(body(tok(1), "Admin", "Alfa1786")))).status == 429 and not auth.is_authorized(1)
    assert len(alerts) == 1 and "Заблокирован" in alerts[0][1]; ok("5 неверных → 429 даже с верным паролем, алерт владельцу отправлен")

    # 6. параллельный перебор
    t2 = tok(2)
    res = await asyncio.gather(*[LOGIN_H(Req(body(t2, "Admin", f"g{i}"))) for i in range(40)])
    assert sum(r.status == 401 for r in res) <= auth.MAX_FAILS and not auth.is_authorized(2); ok("40 параллельных догадок → максимум 5 дошли до проверки")

    # 7. суточный лимит: 15 за сутки блокирует, даже когда 15-минутное окно пусто
    now = time.time()
    db.execute("delete from auth_fails where tg_id=3")
    db.executemany("insert into auth_fails values(3,?)", [(now - 3600 - i,) for i in range(15)])
    assert (await LOGIN_H(Req(body(tok(3)))) ).status == 429 and not auth.is_authorized(3); ok("суточный лимит 15 работает")

    # 8. успешный вход + уведомление; логин без учёта регистра; ссылка одноразовая
    t4 = tok(4); r = await LOGIN_H(Req(body(t4, "admin", "Alfa1786")))
    assert r.status == 200 and auth.is_authorized(4) and logins == [4]
    assert (await LOGIN_H(Req(body(t4)))).status == 400; ok("верные данные → 200, уведомление о входе, повторное использование ссылки → 400")

    # 9. СМЕНА ПАРОЛЯ убивает старые сессии
    old = auth.PW_HASH; auth.PW_HASH = auth.make_hash("Another-Strong-Pass-2026!")
    assert not auth.is_authorized(4); auth.PW_HASH = old
    assert auth.is_authorized(4); ok("смена пароля мгновенно разлогинивает все сессии")
    old_login = auth.LOGIN; auth.LOGIN = "Root"; assert not auth.is_authorized(4); auth.LOGIN = old_login

    # 10. срок сессии, выход, тип tg_id
    db.execute("update auth_sessions set exp=? where tg_id=4", (time.time() - 1,)); assert not auth.is_authorized(4)
    for bad_id in (None, 0, "4", 4.0, True): assert not auth.is_authorized(bad_id)
    ok("истёкшая сессия и нечисловые tg_id → не пускают")

    # 11. совместимость: хэш первой версии (то, что я выдал в прошлом сообщении) всё ещё работает
    import base64
    salt = os.urandom(16); h = hashlib.scrypt(b"Alfa1786", salt=salt, n=2**15, r=8, p=1, maxmem=96*2**20, dklen=32)
    legacy = "scrypt:" + auth._b64(salt) + ":" + auth._b64(h)
    assert auth.check_hash("Alfa1786", legacy) and not auth.check_hash("alfa1786", legacy)
    assert auth.check_hash("Alfa1786", "scrypt:IuTeVUiVxFEt4vJuUwDoaA:PDnmDwqUb8KyY9gMx-Jvxby_Anj2CMsmEEIrMqMhvt4")
    for junk in ("", "scrypt", "scrypt:1:2", "md5:a:b", "scrypt:1:8:2:aa:bb", "scrypt:65536:8:99:aa:bb", "scrypt:65537:8:2:aa:bb"):
        assert not auth.check_hash("x", junk), junk
    ok("хэш из прошлой версии совместим; битые/опасные параметры отвергаются без исключений")

    # 12. конфиг: http запрещён, пустое — ошибка
    for env in ({"PUBLIC_URL": "http://bot.example.com"}, {"PUBLIC_URL": ""}, {"PUBLIC_URL": "javascript:alert(1)"}):
        code = f"""import os,sys;os.environ.update(AUTH_LOGIN='Admin',ALLOW_ORIGIN='x',{', '.join(f'{k}={v!r}' for k,v in env.items())});sys.path.insert(0,{ROOT!r})
import auth;auth.PW_HASH=auth.make_hash('x'*12);auth.require_config();print('STARTED')"""
        import subprocess; o = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert "STARTED" not in o.stdout and o.returncode != 0, env
    ok("PUBLIC_URL по http / пустой / javascript: → сервер не стартует")

    # 13. слабый пароль не примет генератор
    assert not auth._strong_enough("Alfa1786") and not auth._strong_enough("password1234") and auth._strong_enough("Korabl-Luna-2026!")
    ok("генератор хэша отвергает слабые пароли")
    print("\nALL AUTH TESTS PASSED")
asyncio.run(main())
