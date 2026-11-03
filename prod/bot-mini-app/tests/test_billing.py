"""billing.py: сводный отчёт /balance по двум провайдерам — Кими и Яндекс Cloud — одним сообщением.

Проверяется: обе секции и разделитель «______», деньги в формате «1 234,50»,
ветка «ключ не задан», ветка «ошибка запроса» (401/403/500/мусор вместо JSON), изоляция
секций (упавший провайдер не ломает остальные), авторизация Яндекса (Api-Key / OAuth /
IAM-токен из YANDEX_SA_KEY с кэшем) и то, что значения ключей не попадают в текст отчёта.

Секции Render и OpenAI убраны из /balance — их быть не должно: только Кими и Яндекс.

Сеть подменена фейковым сервером (aiohttp.test_utils) — как в test_kimi.py.
Запуск: python3 tests/test_billing.py  (как остальные тесты репозитория)
     или python3 -m pytest tests/test_billing.py.
"""
import asyncio, json, logging, os, re, sys
from contextlib import asynccontextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for k in ("YANDEX_SA_KEY", "YANDEX_SA_KEY_FILE", "YANDEX_OAUTH_TOKEN", "YANDEX_BILLING_API_KEY", "YANDEX_API_KEY"):
    os.environ.pop(k, None)                             # ветка «ключ не задан» должна срабатывать детерминированно
logging.disable(logging.CRITICAL)                       # предупреждения об ошибках не засоряют вывод теста
sys.path.insert(0, ROOT)
import billing, kimi
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

def ok(c): print("  ✓", c)

MODE = {}                                               # ответы фейкового сервера по путям
CALLS = []                                              # (путь, query, заголовки) каждого запроса
BODIES = {}                                             # тела POST-запросов по путям (JWT для IAM-токена)


def route(name):
    async def h(request):
        if request.method == "POST":
            try:
                BODIES[request.path] = await request.json()
            except Exception:
                BODIES[request.path] = {}
        CALLS.append((request.path, dict(request.query), dict(request.headers)))
        m = MODE[name]
        if "raw" in m:                                  # мусор вместо JSON — проверка ветки «не получилось»
            return web.Response(text=m["raw"], status=m.get("code", 200))
        return web.json_response(m["json"], status=m.get("code", 200))
    return h


def make_app():
    """Свежее приложение на каждый запуск: aiohttp привязывает Application к своему event loop,
    а тесты вызывают asyncio.run() несколько раз — каждый со своим циклом."""
    app = web.Application()
    app.router.add_get("/users/me/balance", route("kimi"))
    app.router.add_get("/billing/v1/billingAccounts", route("yandex"))
    app.router.add_get("/billing/v1/billingAccounts/{bid}", route("yandex_get"))
    app.router.add_post("/iam/v1/tokens", route("iam"))
    return app


def defaults():
    """Счастливый сценарий: оба провайдера отвечают."""
    MODE.clear()
    MODE.update(
        kimi={"json": {"data": {"available_balance": 12.5, "voucher_balance": 0, "cash_balance": 10.0}}},
        yandex={"json": {"billingAccounts": [
            {"id": "dn2u7t5", "name": "Мой счёт", "currency": "RUB", "balance": "1234.5", "active": True},
            {"id": "dn8xxx", "name": "", "currency": "RUB", "balance": "0", "active": False},
        ]}},
        yandex_get={"json": {"id": "dn2u7t5", "name": "Мой счёт", "currency": "RUB",
                             "balance": "1234.5", "active": True}},
    )


@asynccontextmanager
async def serve():
    """Фейковый сервер провайдеров; адреса billing.py и kimi.BASE переключаются на него."""
    saved = (billing.YANDEX_URL, billing.IAM_URL, kimi.BASE)
    async with TestClient(TestServer(make_app())) as c:
        root = str(c.make_url("")).rstrip("/")
        billing.YANDEX_URL = root + "/billing/v1/billingAccounts"
        billing.IAM_URL = root + "/iam/v1/tokens"
        billing._iam_cache.update(token="", until=0.0)   # IAM-токены не переходят между тестами
        kimi.BASE = root
        try:
            yield root
        finally:
            (billing.YANDEX_URL, billing.IAM_URL, kimi.BASE) = saved


def env(**kw):
    """Временные переменные окружения; unenv() вернёт всё как было."""
    saved = {k: os.environ.get(k) for k in kw}
    os.environ.update(kw)
    return saved

def unenv(saved):
    for k, v in saved.items():
        if v is None: os.environ.pop(k, None)
        else: os.environ[k] = v


async def report2():
    """Сводка ДВУМЯ секциями — как отдавался /balance до 455bcc5: Кими (report()) + Яндекс (_yandex).
    Секция Яндекса убрана из самого отчёта (это проверяется отдельно в test_full_report), но её
    ветки (ключи, ошибки, IAM) обязаны работать — поэтому тесты зовут секцию напрямую."""
    import aiohttp
    async with aiohttp.ClientSession() as s:
        return await billing.report() + "\n" + billing.SEP + "\n" + await billing._yandex(s)


def test_full_report():
    """Оба провайдера ответили: 2 секции, разделитель «______», деньги и поля на месте."""
    async def run():
        defaults()
        saved_env = env(YANDEX_BILLING_API_KEY="YKEY-111122223333")
        old_kimi, kimi.API_KEY = kimi.API_KEY, "KKEY-444455556666"
        try:
            async with serve():
                text = await report2()
                solo = await billing.report()          # контракт 455bcc5: в /balance секции Яндекса нет
        finally:
            kimi.API_KEY = old_kimi
            unenv(saved_env)
        return text, solo

    # деньги и числа — теми же форматами, что и старый /balance
    assert billing._money("1234.5") == "1 234,50" and billing._money(None) == "—"
    assert billing._num(512) == "512" and billing._num(1.2) == "1,2" and billing._num(None) == "—"
    text, solo = asyncio.run(run())
    assert "☁" not in solo and billing.SEP not in solo, "секция Яндекс Cloud убрана из /balance (455bcc5)"
    ok("/balance отдаёт только секцию Кими — секция Яндекс Cloud убрана (455bcc5)")

    # сводка для проверки веток: две секции, разделённые ровно строкой «______»
    parts = text.split("\n" + billing.SEP + "\n")
    assert len(parts) == 2 and text.count(billing.SEP) == 1
    assert [p.splitlines()[0] for p in parts] == [
        "💳 Кими (Moonshot), CNY:", "☁️ Яндекс Cloud, RUB:"]

    # Кими: те же поля, что и в старом /balance
    assert "• Доступно: 12,50" in parts[0]
    assert "• Ваучеры: 0,00" in parts[0]
    assert "• Основной счёт: 10,00" in parts[0]

    # Яндекс: баланс, валюта и статус каждого счёта
    assert "• Мой счёт (dn2u7t5): 1 234,50 · активен" in parts[1]
    assert "• dn8xxx (dn8xxx): 0,00 · заблокирован" in parts[1]

    # секций Render и OpenAI в отчёте быть не должно
    assert "Render" not in text and "OpenAI" not in text and "🖥" not in text and "🧠" not in text

    # ключи уходят заголовками, но значения не попадают в текст отчёта
    assert any(c[2].get("Authorization") == "Api-Key YKEY-111122223333" for c in CALLS if "billingAccounts" in c[0])
    assert any(c[2].get("Authorization") == "Bearer KKEY-444455556666" for c in CALLS if "users/me/balance" in c[0])
    for secret in ("YKEY-111122223333", "KKEY-444455556666"):
        assert secret not in text
    ok("отчёт: 2 секции (Кими, Яндекс), разделитель «______», деньги и поля на месте, ключи не утекают")


def test_no_keys():
    """Ветка «ключ не задан»: каждая секция честно пишет, какого ключа нет, запросов нет вообще."""
    async def run():
        defaults()
        saved_env = {k: os.environ.pop(k, None) for k in
                     ("YANDEX_SA_KEY", "YANDEX_SA_KEY_FILE", "YANDEX_OAUTH_TOKEN", "YANDEX_BILLING_API_KEY", "YANDEX_API_KEY")}
        old_kimi, kimi.API_KEY = kimi.API_KEY, ""
        try:
            async with serve():
                CALLS.clear()
                text = await report2()
        finally:
            kimi.API_KEY = old_kimi
            unenv(saved_env)
        return text

    text = asyncio.run(run())
    assert not CALLS, "без ключей наружу ничего не должно уходить"
    parts = text.split("\n" + billing.SEP + "\n")
    assert len(parts) == 2
    assert "• ключ не задан (KIMI_API_KEY)" in parts[0]
    assert "• ключ не задан (YANDEX_SA_KEY / YANDEX_OAUTH_TOKEN / YANDEX_API_KEY)" in parts[1]
    ok("без ключей: обе секции про «ключ не задан», наружу ничего не уходит")


def test_errors():
    """Ветка «ошибка запроса»: 403 → подсказка про billing.viewer, 500/мусор → «не получилось»."""
    async def run(out):
        saved_env = env(YANDEX_BILLING_API_KEY="YKEY-111122223333")
        old_kimi, kimi.API_KEY = kimi.API_KEY, "KKEY-444455556666"
        try:
            async with serve():
                out.append(await report2())
        finally:
            kimi.API_KEY = old_kimi
            unenv(saved_env)

    out = []
    defaults()
    MODE["yandex"] = {"code": 403, "json": {}}                      # ключ есть, прав на биллинг нет
    asyncio.run(run(out))
    text = out[0]
    parts = text.split("\n" + billing.SEP + "\n")
    assert len(parts) == 2                                          # упавшая секция не ломает формат отчёта
    assert "• Доступно: 12,50" in parts[0]                          # Кими жив, хотя соседи упали
    assert "• нет доступа: ключу нужна роль billing.accounts.viewer" in parts[1]
    ok("403 → подсказка про billing.viewer; живая секция цела")

    # мусор вместо JSON: та же ветка «не получилось», без исключений наверх
    defaults()
    MODE["yandex"] = {"raw": "не json"}
    asyncio.run(run(out))
    assert "• счета: не получилось (см. лог)" in out[-1].split("\n" + billing.SEP + "\n")[1]
    ok("мусор вместо JSON → «не получилось», отчёт не падает")

    # 500 → «не получилось» с кодом ответа
    defaults()
    MODE["yandex"] = {"code": 500, "json": {}}
    asyncio.run(run(out))
    assert "• счета: не получилось (HTTP 500)" in out[-1].split("\n" + billing.SEP + "\n")[1]
    ok("500 → «не получилось» (HTTP 500), отчёт не падает")

    # 401 → Billing API не принимает Api-Key: подсказка про OAuth-токен
    defaults()
    MODE["yandex"] = {"code": 401, "json": {}}
    asyncio.run(run(out))
    assert "• нет доступа: ключ не принят" in out[-1].split("\n" + billing.SEP + "\n")[1]
    ok("401 → подсказка про YANDEX_SA_KEY / YANDEX_OAUTH_TOKEN (Billing API не берёт Api-Key)")


def test_billing_get():
    """YANDEX_BILLING_ID задан → баланс берётся точечно методом BillingAccount.Get
    (billing/v1/billingAccounts/{id}), а не списком; ответ — сам счёт."""
    async def run():
        defaults()
        saved_env = env(YANDEX_BILLING_API_KEY="***", YANDEX_BILLING_ID="dn2u7t5")
        try:
            async with serve():
                CALLS.clear()
                return await report2()
        finally:
            unenv(saved_env)

    text = asyncio.run(run())
    part = text.split("\n" + billing.SEP + "\n")[1]
    assert "• Мой счёт (dn2u7t5): 1 234,50 · активен" in part, part
    assert [c[0] for c in CALLS if "billingAccounts" in c[0]] == ["/billing/v1/billingAccounts/dn2u7t5"]
    ok("YANDEX_BILLING_ID: баланс через BillingAccount.Get по своему id, ровно один запрос")

    # неверный id → честная подсказка про YANDEX_BILLING_ID, а не пустой экран
    async def run404():
        defaults()
        MODE["yandex_get"] = {"code": 404, "json": {}}
        saved_env = env(YANDEX_BILLING_API_KEY="***", YANDEX_BILLING_ID="dn0nope")
        try:
            async with serve():
                return await report2()
        finally:
            unenv(saved_env)

    part = asyncio.run(run404()).split("\n" + billing.SEP + "\n")[1]
    assert "• счёт dn0nope не найден (проверьте YANDEX_BILLING_ID)" in part, part
    ok("BillingAccount.Get: неверный id → подсказка про YANDEX_BILLING_ID")


def make_sa():
    """Тестовый JSON-ключ сервисного аккаунта: свежая RSA-пара, формат как у key.json Яндекса
    (включая служебную строку PLEASE DO NOT REMOVE рядом с приватным ключом)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    sa = {"id": "ajektest0001", "service_account_id": "ajebtest0001",
          "private_key": "PLEASE DO NOT REMOVE THIS LINE! Yandex.Cloud SA Key ID <ajektest0001>\n" + pem}
    return sa, priv


def test_sa_iam():
    """YANDEX_SA_KEY: *** (PS256) обменивается на IAM-токен,
    в Billing API уходит Bearer; токен кэшируется; приватный ключ не утекает в отчёт."""
    sa, priv = make_sa()

    async def run():
        defaults()
        MODE["iam"] = {"json": {"iamToken": "t1.test-iam", "expiresAt": "2036-01-01T00:00:00Z"}}
        saved_env = env(YANDEX_SA_KEY=json.dumps(sa))
        try:
            async with serve():
                CALLS.clear()
                BODIES.clear()
                first = await report2()
                second = await report2()          # второй раз — IAM-токен из кэша, без обмена
        finally:
            unenv(saved_env)
        return first, second

    first, second = asyncio.run(run())
    parts = first.split("\n" + billing.SEP + "\n")
    assert "• Мой счёт (dn2u7t5): 1 234,50 · активен" in parts[1]   # баланс на месте

    # в Billing API ушёл Bearer с выданным IAM-токеном
    calls_y = [c for c in CALLS if "billingAccounts" in c[0]]
    assert calls_y and all(c[2].get("Authorization") == "Bearer t1.test-iam" for c in calls_y)

    # JWT по документации: PS256, kid ключа, iss — сервисный аккаунт, aud — токен-сервис,
    # exp−iat ≤ 3600, подпись — нашим приватным ключом (проверка публичным)
    import jwt as pyjwt
    assertion = BODIES["/iam/v1/tokens"]["jwt"]
    assert pyjwt.get_unverified_header(assertion) == {"typ": "JWT", "alg": "PS256", "kid": "ajektest0001"}
    claims = pyjwt.decode(assertion, priv.public_key(), algorithms=["PS256"],
                          options={"verify_aud": False})
    assert claims["iss"] == "ajebtest0001" and claims["aud"].endswith("/iam/v1/tokens")
    assert 0 < claims["exp"] - claims["iat"] <= 3600

    # обмен один на два отчёта — токен кэшируется
    assert sum(1 for c in CALLS if c[0].endswith("/iam/v1/tokens")) == 1
    assert "• Мой счёт" in second.split("\n" + billing.SEP + "\n")[1]

    # приватный ключ и токен не утекают в текст отчёта
    assert "PRIVATE KEY" not in first and "t1.test-iam" not in first
    ok("YANDEX_SA_KEY: *** PS256 → IAM-токен → Bearer в Billing API, кэш работает, ключ не утекает")


def test_sa_iam_fallback():
    """IAM-токен не выдался (500) — секция не падает и авторизуется запасным OAuth-токеном."""
    sa, _ = make_sa()

    async def run():
        defaults()
        MODE["iam"] = {"code": 500, "json": {}}
        saved_env = env(YANDEX_SA_KEY=json.dumps(sa), YANDEX_OAUTH_TOKEN="OTOK-777")
        try:
            async with serve():
                CALLS.clear()
                return await report2()
        finally:
            unenv(saved_env)

    text = asyncio.run(run())
    parts = text.split("\n" + billing.SEP + "\n")
    assert "• Мой счёт (dn2u7t5): 1 234,50 · активен" in parts[1]   # баланс цел
    assert any(c[2].get("Authorization") == "OAuth OTOK-777" for c in CALLS if "billingAccounts" in c[0])
    ok("IAM-сервис упал → запасной OAuth-токен, баланс на месте")


if __name__ == "__main__":
    for t in (test_full_report, test_no_keys, test_errors, test_billing_get, test_sa_iam, test_sa_iam_fallback):
        t()
    print("OK")
