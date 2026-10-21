"""Авторизация бота и мини-аппа.

Два независимых входа, в обоих — только пароль (без логина):
  • Telegram-мини-апп: /start → бот шлёт одноразовую ссылку (10 минут, привязана к Telegram-ID) →
    страница /login → пароль → сессия на Telegram-ID;
  • standalone-приложение (APK вне Telegram): только пароль → API-токен (tg_id=0).
    Сессия отдельная от Telegram; данные — та же база того же сервера.
Поле "login" в теле запроса старых клиентов принимается, но игнорируется.
Бот и API пускают только с сессией/токеном.
Пароль в коде и репозитории НЕ хранится — только scrypt-хэш в .env на сервере.
Сгенерировать хэш:  python auth.py   (или  python auth.py --gen  — случайный сильный пароль)
"""
import asyncio, base64, hashlib, hmac, json, logging, os, re, secrets, sys, time
from urllib.parse import urlsplit

PW_HASH = os.getenv("AUTH_PASSWORD_HASH", "").strip()
SESSION_SEC = int(os.getenv("SESSION_DAYS", "30")) * 86400    # «скользящая» сессия: срок продлевается при каждом использовании
LINK_TTL = 600            # ссылка живёт 10 минут
MAX_FAILS = 5             # неверных попыток за SHORT_SEC → блокировка
SHORT_SEC = 900           # 15 минут
DAY_SEC = 86400
DAY_FAILS = 15            # неверных попыток за сутки → блокировка до конца окна

# scrypt: N=2^16, r=8, p=2 (64 МиБ, ~0,4 с) — уровень рекомендаций OWASP. Параметры хранятся в самом хэше.
DEF_PARAMS = (2 ** 16, 8, 2)
LEGACY_PARAMS = (2 ** 15, 8, 1)       # формат «scrypt:соль:хэш» из первой версии — оставлен для совместимости

# ---------- кому вообще можно (белый список) ----------
# Основной признак — @username из Telegram; после первого успешного входа он «прибивается» к числовому Telegram-ID
# (таблица auth_pins), и дальше человека определяет ID: освободивший ник не передаёт доступ тому, кто его займёт.
# Дополнительно можно перечислить числовые ID в ALLOWED_IDS (и/или OWNER_ID) — они допускаются без ника.
DEFAULT_USERNAMES = "Gladik_Vladimir,Gladik_N,hungerrr"
USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}")

def _norm_user(u):
    return (u or "").strip().lstrip("@").casefold()

def _env_usernames():
    raw = os.getenv("ALLOWED_USERNAMES")
    raw = DEFAULT_USERNAMES if raw is None else raw
    return {_norm_user(u) for u in raw.split(",") if USERNAME_RE.fullmatch(_norm_user(u))}

def _env_ids():
    ids = set()
    for x in (os.getenv("ALLOWED_IDS", "") + "," + os.getenv("OWNER_ID", "")).split(","):
        x = x.strip()
        if x.isdigit() and int(x) > 0:
            ids.add(int(x))
    return ids

USERNAMES = _env_usernames()
ALLOWED_IDS = _env_ids()

_b64 = lambda x: base64.urlsafe_b64encode(x).decode().rstrip("=")
_unb64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
_h = lambda t: hashlib.sha256(t.encode()).hexdigest()
_db = None
_on_alert = None
_sem = asyncio.Semaphore(1)           # одновременно считается один scrypt → память ограничена 64 МиБ


# ---------- адрес сервера ----------
def _origin(url):
    """https://host[:port] без пути. Пусто, если адрес некорректен."""
    try:
        sp = urlsplit(url.strip())
        host, port = sp.hostname, sp.port
    except ValueError:
        return ""
    if sp.scheme not in ("https", "http") or not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    default = 443 if sp.scheme == "https" else 80
    return f"{sp.scheme}://{host.lower()}" + (f":{port}" if port and port != default else "")

# Страница входа живёт на backend. Схема «всё с одного домена» — берём WEBAPP_URL,
# для фронта на GitHub Pages обязателен PUBLIC_URL.
BASE = _origin(os.getenv("PUBLIC_URL") or ("" if os.getenv("ALLOW_ORIGIN") else os.getenv("WEBAPP_URL", "")))
_LOCAL = ("localhost", "127.0.0.1", "[::1]")


# ---------- пароль ----------
def make_hash(password, params=DEF_PARAMS):
    n, r, p = params
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * 1024 * 1024, dklen=32)
    return f"scrypt:{n}:{r}:{p}:{_b64(salt)}:{_b64(h)}"      # без «$»: docker compose/systemd не трактуют его как переменную

def _parse(stored):
    parts = stored.split(":")
    if parts[0] != "scrypt": raise ValueError
    if len(parts) == 6:                                    # scrypt:N:r:p:соль:хэш
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, h = parts[4], parts[5]
    elif len(parts) == 3:                                  # scrypt:соль:хэш (первая версия)
        n, r, p = LEGACY_PARAMS
        salt, h = parts[1], parts[2]
    else:
        raise ValueError
    if not (2 ** 14 <= n <= 2 ** 18 and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 8): raise ValueError
    return n, r, p, _unb64(salt), _unb64(h)

def check_hash(password, stored):
    try:
        n, r, p, salt, want = _parse(stored)
        got = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * 1024 * 1024, dklen=len(want))
    except Exception:
        return False
    return hmac.compare_digest(got, want)

def check_credentials(password):
    """Вход только по паролю: логин не требуется и не проверяется."""
    return check_hash(password, PW_HASH)

def _fp():
    """Отпечаток текущего пароля. Сменили пароль → все старые сессии разом недействительны."""
    return hashlib.sha256(PW_HASH.encode()).hexdigest()[:32]

def require_config():
    miss = []
    if not USERNAMES and not ALLOWED_IDS: miss.append("ALLOWED_USERNAMES или ALLOWED_IDS (кому разрешён доступ)")
    try: _parse(PW_HASH)
    except Exception: miss.append("AUTH_PASSWORD_HASH (получить: python auth.py)")
    if not BASE: miss.append("PUBLIC_URL (https-адрес backend)")
    if miss:
        sys.exit("Авторизация не настроена. Задайте в .env: " + ", ".join(miss))
    if BASE.startswith("http://") and urlsplit(BASE).hostname not in ("localhost", "127.0.0.1", "::1"):
        sys.exit("PUBLIC_URL должен быть https:// — по http пароль уйдёт по сети открытым текстом.")


# ---------- БД: ссылки, сессии, неудачные попытки (запросы только с параметрами) ----------
def init(db):
    global _db
    _db = db
    db.executescript("""
create table if not exists auth_links(h text primary key, tg_id integer not null, exp real not null, fails integer not null default 0, uname text);
create table if not exists auth_pins(username text primary key, tg_id integer not null);
create table if not exists auth_sessions(tg_id integer primary key, exp real not null);
create table if not exists auth_fails(tg_id integer not null, ts real not null);
create table if not exists api_tokens(h text primary key, tg_id integer not null, exp real not null);
create table if not exists auth_tg(code_h text primary key, poll_h text not null, exp real not null, tg_id integer);
""")
    for stmt in ("alter table auth_sessions add column fp text",       # сессии версии 1 без отпечатка — недействительны
                 "alter table auth_links add column uname text",
                 "alter table api_tokens add column fp text"):        # токены без отпечатка — недействительны
        try:
            db.execute(stmt)
        except Exception:
            pass
    db.commit()

def _pinned_ok(tg_id):
    """ID закреплён за ником, который и сейчас есть в белом списке."""
    return any(r[0] in USERNAMES for r in _db.execute("select username from auth_pins where tg_id=?", (tg_id,)))

def allowed(tg_id, username=None):
    """Можно ли этому Telegram-пользователю вообще пользоваться ботом/кабинетом (до проверки пароля)."""
    if type(tg_id) is not int or tg_id <= 0:
        return False
    if tg_id in ALLOWED_IDS or _pinned_ok(tg_id):
        return True
    u = _norm_user(username if isinstance(username, str) else "")
    if u and u in USERNAMES:
        r = _db.execute("select tg_id from auth_pins where username=?", (u,)).fetchone()
        return r is None or r[0] == tg_id          # ник уже закреплён за другим ID → это не он
    return False

def _pin(tg_id, username):
    u = _norm_user(username)
    if u in USERNAMES:
        _db.execute("insert or replace into auth_pins(username, tg_id) values(?,?)", (u, tg_id))

def is_authorized(tg_id):
    """Пускает только тех, кто и сейчас в белом списке и имеет живую сессию. Сессия скользящая:
    пока человек пользуется ботом, пароль повторно не спрашивается."""
    if type(tg_id) is not int or tg_id <= 0:
        return False
    if not (tg_id in ALLOWED_IDS or _pinned_ok(tg_id)):
        return False
    now = time.time()
    row = _db.execute("select exp from auth_sessions where tg_id=? and exp>? and fp=?", (tg_id, now, _fp())).fetchone()
    if row is None:
        return False
    if row[0] < now + SESSION_SEC - 3600:                      # продлеваем не чаще раза в час
        _db.execute("update auth_sessions set exp=? where tg_id=?", (now + SESSION_SEC, tg_id)); _db.commit()
    return True

def logout(tg_id):
    _db.execute("delete from auth_sessions where tg_id=?", (tg_id,))
    _db.execute("delete from api_tokens where tg_id=?", (tg_id,))       # и токены standalone-приложения
    _db.commit()

# ---------- API-токены для standalone-приложения (APK вне Telegram) ----------
# Вход тот же (одноразовая ссылка из бота + пароль), но приложение получает токен
# и передаёт его в заголовке X-Session вместо подписи Telegram initData.
# В БД — только хэш токена; срок как у сессии, продлевается при использовании.
def issue_token(tg_id):
    now = time.time()
    t = secrets.token_urlsafe(32)
    _db.execute("delete from api_tokens where exp<?", (now,))
    _db.execute("insert or replace into api_tokens(h, tg_id, exp, fp) values(?,?,?,?)",
                (_h(t), tg_id, now + SESSION_SEC, _fp()))
    _db.commit()
    return t

def check_token(raw):
    """tg_id (0 — standalone-пользователь APK) — токен валиден; иначе None.
    Telegram-токены дополнительно сверяются с белым списком; standalone-токены —
    только с отпечатком учётных данных (смена пароля гасит их)."""
    if not isinstance(raw, str) or not TOKEN_RE.fullmatch(raw):
        return None
    now, h = time.time(), _h(raw)
    row = _db.execute("select tg_id, exp, fp from api_tokens where h=?", (h,)).fetchone()
    if row is None or row[1] < now or (row[2] or "") != _fp() or (row[0] != STANDALONE_UID and not allowed(row[0])):
        if row is not None:                                        # истёк, сменился пароль или исключён из списка — токен гасим
            _db.execute("delete from api_tokens where h=?", (h,)); _db.commit()
        return None
    if row[1] < now + SESSION_SEC - 3600:                          # продлеваем не чаще раза в час
        _db.execute("update api_tokens set exp=? where h=?", (now + SESSION_SEC, h)); _db.commit()
    return row[0]

def new_link(tg_id, username=""):
    """Одноразовая ссылка. Токен в #фрагменте: не попадает ни в логи сервера, ни в Referer."""
    now, t = time.time(), secrets.token_urlsafe(32)
    _db.execute("delete from auth_links where exp<? or tg_id=?", (now, tg_id))   # старые ссылки гасим
    _db.execute("insert into auth_links(h, tg_id, exp, uname) values(?,?,?,?)",
                (_h(t), tg_id, now + LINK_TTL, _norm_user(username)))   # в БД — только хэш
    _db.commit()
    return f"{BASE}/login#{t}"

def _fail_counts(tg_id, now):
    _db.execute("delete from auth_fails where ts<?", (now - DAY_SEC,))
    short = _db.execute("select count(*) from auth_fails where tg_id=? and ts>?", (tg_id, now - SHORT_SEC)).fetchone()[0]
    day = _db.execute("select count(*) from auth_fails where tg_id=?", (tg_id,)).fetchone()[0]
    return short, day

def _locked(tg_id, now):
    short, day = _fail_counts(tg_id, now)
    return short >= MAX_FAILS or day >= DAY_FAILS

async def _alert(tg_id, text):
    if _on_alert:
        try: await _on_alert(tg_id, text)
        except Exception: pass                                  # сбой уведомления не должен ломать вход

log = logging.getLogger("tutor.auth")

async def attempt(token, password):
    """Одна попытка входа → (http-код, текст ошибки, tg_id)."""
    now = time.time()
    row = _db.execute("select tg_id, exp, fails, uname from auth_links where h=?", (_h(token),)).fetchone()
    if not row or row[1] < now:
        return 400, "Ссылка устарела. Вернитесь в бота и нажмите /start.", 0
    tg_id, uname = row[0], row[3] or ""
    if not allowed(tg_id, uname):                              # список мог измениться после выдачи ссылки
        _db.execute("delete from auth_links where h=?", (_h(token),)); _db.commit()
        log.warning("login refused: tg_id=%s not in allowlist", tg_id)
        return 403, "Доступ закрыт.", 0
    if _locked(tg_id, now):
        log.warning("login blocked (too many attempts): tg_id=%s", tg_id)
        return 429, "Слишком много попыток. Подождите и запросите новую ссылку позже.", tg_id
    # попытка засчитывается ДО проверки (между проверкой лимита и записью нет await) —
    # параллельный залп запросов не обойдёт лимит
    _db.execute("insert into auth_fails(tg_id, ts) values(?,?)", (tg_id, now))
    if row[2] + 1 >= MAX_FAILS:
        _db.execute("delete from auth_links where h=?", (_h(token),))            # ссылка сгорает
    else:
        _db.execute("update auth_links set fails=fails+1 where h=?", (_h(token),))
    _db.commit()
    async with _sem:
        ok = await asyncio.get_running_loop().run_in_executor(None, check_credentials, password)
    if not ok:
        if _locked(tg_id, time.time()):
            await _alert(tg_id, "⚠️ Заблокирован вход: слишком много неверных паролей. "
                                "Если это были не вы — смените пароль на сервере.")
        log.warning("login failed: tg_id=%s", tg_id)
        return 401, "Неверный пароль.", tg_id
    _db.execute("delete from auth_links where tg_id=?", (tg_id,))
    _db.execute("delete from auth_fails where tg_id=?", (tg_id,))
    _db.execute("delete from auth_sessions where exp<?", (now,))
    _pin(tg_id, uname)                                         # ник → числовой ID
    _db.execute("insert or replace into auth_sessions(tg_id, exp, fp) values(?,?,?)", (tg_id, now + SESSION_SEC, _fp()))
    _db.commit()
    log.info("login ok: tg_id=%s", tg_id)
    return 200, "", tg_id


# ---------- standalone-вход (APK вне Telegram): только пароль ----------
# Не зависит от Telegram и мини-аппа: нет ни ссылки из бота, ни Telegram-ID.
# Учёт неудачных попыток — в общем «ведре» tg_id=0; токен сессии привязан к
# отпечатку пароля и гаснет при смене пароля.
STANDALONE_UID = 0

async def attempt_standalone(password):
    """Одна попытка standalone-входа → (http-код, текст ошибки, tg_id=0)."""
    now, uid = time.time(), STANDALONE_UID
    if _locked(uid, now):
        log.warning("standalone login blocked (too many attempts)")
        return 429, "Слишком много попыток. Подождите немного и попробуйте снова.", uid
    # попытка засчитывается ДО проверки — параллельный залп не обойдёт лимит
    _db.execute("insert into auth_fails(tg_id, ts) values(?,?)", (uid, now))
    _db.commit()
    async with _sem:
        ok = await asyncio.get_running_loop().run_in_executor(None, check_credentials, password)
    if not ok:
        if _locked(uid, time.time()):
            await _alert(uid, "⚠️ Заблокирован вход в приложение: слишком много неверных паролей. "
                              "Если это были не вы — смените пароль на сервере.")
        log.warning("standalone login failed")
        return 401, "Неверный пароль.", uid
    _db.execute("delete from auth_fails where tg_id=?", (uid,))
    _db.commit()
    log.info("standalone login ok")
    return 200, "", uid


# ---------- быстрый вход в APK через Telegram (без пароля) ----------
# 1) приложение просит /api/tglogin/start → получает ссылку на бота (код) и свой секрет опроса;
# 2) в боте человек жмёт «Подтвердить» → tg_confirm() привязывает заявку к его Telegram-ID
#    (только если он в белом списке);
# 3) приложение опрашивает /api/tglogin/poll по секрету и получает тот же API-токен, что и при входе по паролю.
# Код (уходит в Telegram) и секрет опроса (остаётся в приложении) — разные значения: кто увидел ссылку, токен не получит.
TG_TTL = 300              # заявка живёт 5 минут
TG_MAX_PENDING = 200      # защита от заваливания таблицы
TG_GRACE = 30             # после выдачи токена заявка живёт ещё столько секунд (потерянный ответ)
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")   # бот заполняет сам при старте (get_me)
APP_ENV = os.getenv("APP_ENV", "").strip().lower()
# схема возврата в APK: dev-сборка и prod-сборка стоят на одном телефоне — схемы разные
APK_SCHEME = os.getenv("APK_SCHEME", "").strip() or ("kabinet-dev" if APP_ENV == "dev" else "kabinet")
TG_CODE_RE = re.compile(r"[A-Za-z0-9_-]{16,40}")

def tg_start():
    """Новая заявка → (код для ссылки бота, секрет опроса) или None, если очередь переполнена."""
    now = time.time()
    _db.execute("delete from auth_tg where exp<?", (now,))
    if _db.execute("select count(*) from auth_tg").fetchone()[0] >= TG_MAX_PENDING:
        return None
    code, poll = secrets.token_urlsafe(16), secrets.token_urlsafe(32)
    _db.execute("insert into auth_tg(code_h, poll_h, exp, tg_id) values(?,?,?,null)", (_h(code), _h(poll), now + TG_TTL))
    _db.commit()
    return code, poll

def tg_confirm(code, tg_id, username=""):
    """Бот: человек подтвердил вход. True — заявка привязана к его ID."""
    if not isinstance(code, str) or not TG_CODE_RE.fullmatch(code) or not allowed(tg_id, username):
        return False
    now = time.time()
    cur = _db.execute("update auth_tg set tg_id=? where code_h=? and exp>? and tg_id is null", (tg_id, _h(code), now))
    if cur.rowcount != 1:
        return False
    _pin(tg_id, username)                                      # ник → числовой ID, как при входе по паролю
    _db.commit()
    log.info("tg login confirmed: tg_id=%s", tg_id)
    return True

def tg_cancel(code):
    if isinstance(code, str) and TG_CODE_RE.fullmatch(code):
        _db.execute("delete from auth_tg where code_h=? and tg_id is null", (_h(code),)); _db.commit()

def tg_poll(poll):
    """Приложение: (статус, токен). pending — ждём подтверждения; ok — токен выдан (один раз); gone — истекла/неизвестна."""
    if not isinstance(poll, str) or not TOKEN_RE.fullmatch(poll):
        return "gone", None
    now = time.time()
    row = _db.execute("select exp, tg_id from auth_tg where poll_h=?", (_h(poll),)).fetchone()
    if row is None or row[0] < now:
        if row is not None:
            _db.execute("delete from auth_tg where poll_h=?", (_h(poll),)); _db.commit()
        return "gone", None
    if row[1] is None:
        return "pending", None
    if not allowed(row[1]):                                             # список мог измениться за это время
        _db.execute("delete from auth_tg where poll_h=?", (_h(poll),)); _db.commit()
        return "gone", None
    # Заявка закрывается не сразу, а через TG_GRACE с: если страница перезагрузилась и ответ с токеном потерялся
    # (возврат из Telegram), тот же держатель секрета опроса получит новый токен. Подтвердить заявку заново нельзя.
    _db.execute("update auth_tg set exp=? where poll_h=?", (min(row[0], now + TG_GRACE), _h(poll)))
    _db.commit()
    return "ok", issue_token(row[1])

def tg_link(code):
    """(tg://-ссылка, https-ссылка) на бота с кодом заявки."""
    return (f"tg://resolve?domain={BOT_USERNAME}&start=login_{code}",
            f"https://t.me/{BOT_USERNAME}?start=login_{code}")


# ---------- разбор запроса (чистая функция — легко тестировать) ----------
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

def parse_login(raw):
    """Байты тела → (токен | None, пароль) или ValueError.
    Токен опущен или пуст → standalone-вход (APK): только пароль, без Telegram.
    Поле "login" (если прислано старым клиентом) игнорируется — вход только по паролю."""
    try:
        j = json.loads(raw)
    except RecursionError:                                         # «[[[[…» в 4 КБ
        raise ValueError
    if not isinstance(j, dict):
        raise ValueError
    t, pw = j.get("t"), j.get("password")
    if not isinstance(pw, str):
        raise ValueError
    if t is not None and (not isinstance(t, str) or not TOKEN_RE.fullmatch(t)):
        raise ValueError                                            # токен передан, но неверного формата
    if t == "":
        t = None
    if not 0 < len(pw) <= 128 or CTRL_RE.search(pw):
        raise ValueError
    pw.encode()                                                    # одиночные суррогаты → UnicodeError ⊂ ValueError
    return t, pw


# ---------- HTTP: страница и обработчик входа ----------
PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex">
<title>Вход</title><style nonce="__N__">
:root{--bg:#fff;--fg:#17212b;--mut:#6b7785;--card:#f3f5f8;--bd:#d5dbe3;--ac:#2481cc;--er:#d93025}
@media(prefers-color-scheme:dark){:root{--bg:#17212b;--fg:#f5f5f5;--mut:#8b98a5;--card:#1f2c3a;--bd:#2f3e4e;--ac:#4aa3e8;--er:#ff6b60}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{width:100%;max-width:360px}h1{font-size:22px;margin:0 0 4px}p{margin:0 0 18px;color:var(--mut);font-size:14px}
label{display:block;font-size:13px;color:var(--mut);margin:12px 0 4px}
input{width:100%;padding:12px;font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--bd);border-radius:10px}
input:focus{outline:2px solid var(--ac);border-color:transparent}
button{width:100%;margin-top:18px;padding:13px;font:inherit;font-weight:600;color:#fff;background:var(--ac);border:0;border-radius:10px}
button:disabled{opacity:.6}#msg{min-height:20px;margin-top:12px;font-size:14px;color:var(--er)}#msg.ok{color:var(--ac)}
</style></head><body><main><h1>🔐 Вход</h1><p>Кабинет репетитора</p>
<form id="f" method="post" action="/login">
<label for="p">Пароль</label><input id="p" type="text" autocomplete="current-password" maxlength="128" required>
<button id="b" type="submit">Войти</button><div id="msg" role="alert"></div></form></main>
<script nonce="__N__">(()=>{const t=location.hash.slice(1);history.replaceState(null,"",location.pathname);
const $=i=>document.getElementById(i),say=(s,ok)=>{$("msg").textContent=s;$("msg").className=ok?"ok":""};
$("f").addEventListener("submit",async e=>{e.preventDefault();
if(!t){say("Откройте ссылку из бота заново: нажмите /start");return}
$("b").disabled=true;say("");
try{const r=await fetch("/api/login",{method:"POST",credentials:"omit",cache:"no-store",headers:{"Content-Type":"application/json"},
body:JSON.stringify({t,password:$("p").value})});
const j=await r.json().catch(()=>({}));
if(r.ok){$("f").querySelectorAll("input,button").forEach(x=>x.disabled=true);$("p").value="";
say("✅ Вход выполнен. Вернитесь в Telegram.",true);setTimeout(()=>window.close(),1500);return}
say(j.error||"Ошибка. Попробуйте ещё раз.");}catch(_){say("Нет связи с сервером.")}
$("p").value="";$("b").disabled=false});})();</script></body></html>"""

# Страница «вернуться в приложение»: кнопка бота открывает её, а она отдаёт управление APK по схеме kabinet://
BACK_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>Вход выполнен</title>
<style nonce="__N__">body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;background:#fff;color:#17212b;font:16px/1.4 system-ui,sans-serif;text-align:center}
a{display:block;margin-top:18px;padding:13px 22px;font-weight:600;color:#fff;background:#0F2E6E;border-radius:10px;text-decoration:none}p{color:#6b7785;font-size:14px}</style></head>
<body><main><h1>✅ Вход подтверждён</h1><p>Возвращаемся в приложение…</p>
<a id="a" href="__S__://login">Открыть приложение</a></main>
<script nonce="__N__">setTimeout(()=>{location.href="__S__://login"},300)</script></body></html>"""

SEC = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
       "X-Frame-Options": "DENY", "Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Resource-Policy": "same-origin",
       "Permissions-Policy": "geolocation=(), camera=(), microphone=()"}

def setup(app, on_login=None, on_alert=None):
    """Добавляет GET /login и POST /api/login.
    on_login(tg_id) — async-уведомление об успешном входе; on_alert(tg_id, text) — о блокировке."""
    global _on_alert
    from aiohttp import web
    _on_alert = on_alert
    headers = dict(SEC)
    if BASE.startswith("https://"):
        headers["Strict-Transport-Security"] = "max-age=31536000"

    def out(code, msg="", token=None):
        body = {"ok": code == 200, "error": msg}
        if token:
            body["token"] = token                              # для standalone-приложения; веб-страница поле игнорирует
        return web.json_response(body, status=code, headers=headers)

    async def page(request):
        n = secrets.token_urlsafe(16)
        csp = (f"default-src 'none'; script-src 'nonce-{n}'; style-src 'nonce-{n}'; connect-src 'self'; "
               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        return web.Response(text=PAGE.replace("__N__", n), content_type="text/html", charset="utf-8",
                            headers={**headers, "Content-Security-Policy": csp})

    async def login(request):
        # CSRF / чужие страницы: только запрос со страницы входа этого же сервера
        if request.headers.get("Origin") != BASE or request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
            return out(403, "Запрещено")
        if request.content_type != "application/json" or not (0 < (request.content_length or 0) <= 4096):
            return out(400, "Неверный запрос")
        try:
            t, pw = parse_login(await request.read())
        except ValueError:
            return out(400, "Проверьте введённые данные")
        if t:
            code, msg, tg_id = await attempt(t, pw)                   # вход из Telegram по одноразовой ссылке
        else:
            code, msg, tg_id = await attempt_standalone(pw)           # standalone (APK): только пароль
        if code == 200 and on_login and tg_id:
            try: await on_login(tg_id)
            except Exception: pass                                 # сбой уведомления не отменяет вход
        return out(code, msg, issue_token(tg_id) if code == 200 else None)

    def _same_origin(request):
        return request.headers.get("Origin") == BASE and request.headers.get("Sec-Fetch-Site", "same-origin") == "same-origin"

    async def tg_start_h(request):
        if not _same_origin(request) or not BOT_USERNAME:
            return web.json_response({"ok": False, "error": "Запрещено"}, status=403, headers=headers)
        r = tg_start()
        if r is None:
            return web.json_response({"ok": False, "error": "Слишком много входов, подождите минуту."}, status=429, headers=headers)
        code, poll = r
        tgl, web_l = tg_link(code)
        return web.json_response({"ok": True, "poll": poll, "tg": tgl, "url": web_l, "ttl": TG_TTL}, headers=headers)

    async def tg_poll_h(request):
        if not _same_origin(request) or request.content_type != "application/json" or not (0 < (request.content_length or 0) <= 512):
            return web.json_response({"ok": False, "error": "Запрещено"}, status=403, headers=headers)
        try:
            j = json.loads(await request.read())
            poll = j.get("poll") if isinstance(j, dict) else None
        except (ValueError, RecursionError):
            poll = None
        st, tok = tg_poll(poll)
        return web.json_response({"ok": st == "ok", "status": st, **({"token": tok} if tok else {})},
                                 status=200 if st != "gone" else 410, headers=headers)

    async def back(request):
        n = secrets.token_urlsafe(16)
        csp = (f"default-src 'none'; script-src 'nonce-{n}'; style-src 'nonce-{n}'; base-uri 'none'; "
               "form-action 'none'; frame-ancestors 'none'")
        return web.Response(text=BACK_PAGE.replace("__N__", n).replace("__S__", APK_SCHEME), content_type="text/html",
                            charset="utf-8", headers={**headers, "Content-Security-Policy": csp})

    app.router.add_get("/login", page)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/tglogin/start", tg_start_h)
    app.router.add_post("/api/tglogin/poll", tg_poll_h)
    app.router.add_get("/app-return", back)


# ---------- генератор хэша ----------
WEAK = {"password", "qwerty", "admin", "12345678", "123456789", "1234567890", "qwerty123", "password1", "admin123"}

def _strong_enough(p):
    kinds = sum(bool(re.search(x, p)) for x in (r"[a-zа-яё]", r"[A-ZА-ЯЁ]", r"\d", r"[^\w\s]"))
    return len(p) >= 12 and kinds >= 3 and p.casefold() not in WEAK

if __name__ == "__main__":
    import getpass
    if "--gen" in sys.argv:
        alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789-_"
        p = "".join(secrets.choice(alphabet) for _ in range(18))
        print("Новый пароль (сохраните в менеджере паролей, больше он нигде не показывается):\n" + p)
    else:
        p = getpass.getpass("Пароль: ")
        if p != getpass.getpass("Повторите: "):
            sys.exit("Пароли не совпали")
        if not _strong_enough(p):
            sys.exit("Слабый пароль: нужно от 12 символов и минимум 3 вида (строчные, ЗАГЛАВНЫЕ, цифры, символы). "
                     "Или запустите: python auth.py --gen")
    print("\nДобавьте в .env на сервере:\nAUTH_PASSWORD_HASH=" + make_hash(p))
    print("\nПосле смены перезапустите бота — все старые входы станут недействительными.")
