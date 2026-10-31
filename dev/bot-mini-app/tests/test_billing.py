"""billing.py: сводный отчёт /balance по четырём провайдерам одним сообщением.

Проверяется: все четыре секции и разделители «______», деньги в формате «1 234,50»,
ветка «ключ не задан», ветка «ошибка запроса» (403/500/мусор вместо JSON), изоляция
секций (упавший провайдер не ломает остальные), разбор таймсерий Render (числа и
объекты {startTime|timestamp, value}), параметры bandwidth (startTime/endTime) и
то, что значения ключей не попадают в текст отчёта.

Сеть подменена фейковым сервером (aiohttp.test_utils) — как в test_kimi.py.
Запуск: python3 tests/test_billing.py  (как остальные тесты репозитория)
     или python3 -m pytest tests/test_billing.py.
"""
import asyncio, logging, os, re, sys
from contextlib import asynccontextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for k in ("YANDEX_OAUTH_TOKEN", "YANDEX_BILLING_API_KEY", "YANDEX_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "WHISPER_API_KEY", "RENDER_API_KEY"):
    os.environ.pop(k, None)                             # ветка «ключ не задан» должна срабатывать детерминированно
logging.disable(logging.CRITICAL)                       # предупреждения об ошибках не засоряют вывод теста
sys.path.insert(0, ROOT)
import billing, kimi
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

def ok(c): print("  ✓", c)

MODE = {}                                               # ответы фейкового сервера по путям
CALLS = []                                              # (путь, query, заголовки) каждого запроса


def route(name):
    async def h(request):
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
    app.router.add_get("/v1/dashboard/billing/credit_grants", route("oa_credit"))
    app.router.add_get("/v1/dashboard/billing/usage", route("oa_usage"))
    app.router.add_get("/v1/organization/costs", route("oa_costs"))
    app.router.add_get("/v1/services", route("services"))
    for p in ("cpu-limit", "memory-limit", "disk-usage", "disk-capacity", "bandwidth"):
        app.router.add_get(f"/v1/metrics/{p}", route(p))
    return app


def defaults():
    """Счастливый сценарий: все провайдеры отвечают, метрики — в обоих форматах значений."""
    MODE.clear()
    MODE.update(
        kimi={"json": {"data": {"available_balance": 12.5, "voucher_balance": 0, "cash_balance": 10.0}}},
        yandex={"json": {"billingAccounts": [
            {"id": "dn2u7t5", "name": "Мой счёт", "currency": "RUB", "balance": "1234.5", "active": True},
            {"id": "dn8xxx", "name": "", "currency": "RUB", "balance": "0", "active": False},
        ]}},
        oa_credit={"json": {"object": "credit_grants", "total_granted": 18.0, "total_used": 10.0,
                           "total_available": 8.0}},
        oa_usage={"json": {"object": "usage", "total_usage": 15}},   # legacy отдаёт ЦЕНТЫ: 15 = $0,15
        oa_costs={"json": {"data": [{"object": "bucket", "results": [
            {"amount": {"value": 0.15, "currency": "USD"}}, {"amount": {"value": 0.05, "currency": "USD"}}]}]}},
        services={"json": [
            {"cursor": "c1", "service": {"id": "srv-abc", "name": "tutor-bot", "type": "web_service",
                                          "serviceDetails": {"plan": "starter", "numInstances": 1}}},
        ]},
        # значения таймсерий специально в трёх видах: просто числа, {startTime, value}, {timestamp, value}
        **{
            "cpu-limit": {"json": [{"labels": [{"field": "service", "value": "srv-abc"}], "values": [500, 512],
                                    "unit": "millicores"}]},
            "memory-limit": {"json": [{"labels": [], "values": [{"startTime": "2026-10-01T00:00:00Z", "value": 400},
                                                                {"startTime": "2026-10-02T00:00:00Z", "value": 512}],
                                       "unit": "MB"}]},
            "disk-usage": {"json": [{"labels": [], "values": [{"timestamp": "2026-10-02T00:00:00Z", "value": 123}],
                                     "unit": "MB"}]},
            "disk-capacity": {"json": [{"labels": [], "values": [1], "unit": "GB"}]},
            "bandwidth": {"json": [{"labels": [], "values": [{"value": 1.2}], "unit": "GB"}]},
        },
    )


@asynccontextmanager
async def serve():
    """Фейковый сервер провайдеров; адреса billing.py и kimi.BASE переключаются на него."""
    saved = (billing.YANDEX_URL, billing.OA_CREDIT, billing.OA_USAGE, billing.OA_COSTS,
             billing.RENDER_URL, kimi.BASE)
    async with TestClient(TestServer(make_app())) as c:
        root = str(c.make_url("")).rstrip("/")
        billing.YANDEX_URL = root + "/billing/v1/billingAccounts"
        billing.OA_CREDIT = root + "/v1/dashboard/billing/credit_grants"
        billing.OA_USAGE = root + "/v1/dashboard/billing/usage"
        billing.OA_COSTS = root + "/v1/organization/costs"
        billing.RENDER_URL = root + "/v1"
        kimi.BASE = root
        try:
            yield root
        finally:
            (billing.YANDEX_URL, billing.OA_CREDIT, billing.OA_USAGE, billing.OA_COSTS,
             billing.RENDER_URL, kimi.BASE) = saved


def env(**kw):
    """Временные переменные окружения; unenv() вернёт всё как было."""
    saved = {k: os.environ.get(k) for k in kw}
    os.environ.update(kw)
    return saved

def unenv(saved):
    for k, v in saved.items():
        if v is None: os.environ.pop(k, None)
        else: os.environ[k] = v


def test_full_report():
    """Все провайдеры ответили: 4 секции, разделители «______», деньги и поля на месте."""
    async def run():
        defaults()
        saved_env = env(YANDEX_BILLING_API_KEY="YKEY-111122223333", WHISPER_API_KEY="OKEY-9876543210",
            OPENAI_API_KEY="AKEY2-000011112222", OPENAI_ADMIN_KEY="AKEY-1234567890", RENDER_API_KEY="RKEY-555566667777")
        old_kimi, kimi.API_KEY = kimi.API_KEY, "KKEY"
        try:
            async with serve():
                text = await billing.report()
        finally:
            kimi.API_KEY = old_kimi
            unenv(saved_env)
        return text

    # деньги и числа — теми же форматами, что и старый /balance
    assert billing._money("1234.5") == "1 234,50" and billing._money(None) == "—"
    assert billing._num(512) == "512" and billing._num(1.2) == "1,2" and billing._num(None) == "—"
    text = asyncio.run(run())

    # одно сообщение: четыре секции, разделённые ровно строкой «______»
    parts = text.split("\n" + billing.SEP + "\n")
    assert len(parts) == 4 and text.count(billing.SEP) == 3
    assert [p.splitlines()[0] for p in parts] == [
        "💳 Кими (Moonshot), CNY:", "☁️ Яндекс Cloud, RUB:", "🧠 OpenAI, USD:", "🖥 Render, USD:"]

    # Кими: те же поля, что и в старом /balance
    assert "• Доступно: 12,50" in parts[0]
    assert "• Ваучеры: 0,00" in parts[0]
    assert "• Основной счёт: 10,00" in parts[0]

    # Яндекс: баланс, валюта и статус каждого счёта
    assert "• Мой счёт (dn2u7t5): 1 234,50 · активен" in parts[1]
    assert "• dn8xxx (dn8xxx): 0,00 · заблокирован" in parts[1]

    # OpenAI: какой ключ подключён (хвост), кредиты, расход с начала месяца (Costs API, Admin-ключ)
    assert "• Ключ: WHISPER_API_KEY …3210" in parts[2]   # приоритет у ключа голоса: OPENAI_API_KEY задан, но не перекрывает
    assert "• Кредиты: доступно 8,00 · использовано 10,00 из 18,00" in parts[2]
    assert "• Расход с начала месяца: 0,20" in parts[2]

    # Render: сервис с планом, лимиты (последняя точка таймсерии в любом формате значений), диск «из»
    assert "• tutor-bot (srv-abc): план starter" in parts[3]
    assert "  · CPU-лимит: 512 millicores" in parts[3]
    assert "  · Память: 512 MB" in parts[3]
    assert "  · Диск: 123 MB из 1 GB" in parts[3]
    assert "  · Трафик с начала месяца: 1,2 GB" in parts[3]
    assert "• Даты списания: публичный API Render не отдаёт" in parts[3]

    # трафик запрашивается за период с начала месяца (UTC) и с фильтром по сервису
    path, query, headers = next(c for c in CALLS if c[0] == "/v1/metrics/bandwidth")
    assert query.get("resource") == "srv-abc"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", query.get("startTime", ""))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", query.get("endTime", ""))

    # ключи уходят заголовками, но значения не попадают в текст отчёта
    assert any(c[2].get("Authorization") == "Bearer RKEY-555566667777" for c in CALLS if c[0] == "/v1/services")
    assert any(c[2].get("Authorization") == "Api-Key YKEY-111122223333" for c in CALLS if "billingAccounts" in c[0])
    for secret in ("YKEY-111122223333", "OKEY-9876543210", "AKEY-1234567890", "AKEY2-000011112222",
                   "RKEY-555566667777", "KKEY-444455556666"):
        assert secret not in text
    ok("отчёт: 4 секции, разделители «______», деньги и поля как в старом /balance, ключи не утекают")


def test_no_keys():
    """Ветка «ключ не задан»: каждая секция честно пишет, какого ключа нет, запросов нет вообще."""
    async def run():
        defaults()
        # ключи вычищены ещё при импорте модуля — здесь только сохраняем и возвращаем как было
        saved_env = {k: os.environ.pop(k, None) for k in
                     ("YANDEX_OAUTH_TOKEN", "YANDEX_BILLING_API_KEY", "YANDEX_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "WHISPER_API_KEY", "RENDER_API_KEY")}
        old_kimi, kimi.API_KEY = kimi.API_KEY, ""
        try:
            async with serve():
                CALLS.clear()
                text = await billing.report()
        finally:
            kimi.API_KEY = old_kimi
            unenv(saved_env)
        return text

    text = asyncio.run(run())
    assert not CALLS, "без ключей наружу ничего не должно уходить"
    parts = text.split("\n" + billing.SEP + "\n")
    assert len(parts) == 4
    assert "• ключ не задан (KIMI_API_KEY)" in parts[0]
    assert "• ключ не задан (YANDEX_OAUTH_TOKEN / YANDEX_API_KEY)" in parts[1]
    assert "• ключ не задан (WHISPER_API_KEY / OPENAI_API_KEY)" in parts[2]
    assert "• ключ не задан (RENDER_API_KEY)" in parts[3]
    ok("без ключей: все секции про «ключ не задан», наружу ничего не уходит")


def test_errors():
    """Ветка «ошибка запроса»: 403 → подсказка про billing.viewer, 500/мусор → «не получилось»."""
    async def run(out):
        saved_env = env(YANDEX_BILLING_API_KEY="YKEY-111122223333", WHISPER_API_KEY="OKEY-9876543210",
            OPENAI_ADMIN_KEY="AKEY-1234567890", RENDER_API_KEY="RKEY-555566667777")
        old_kimi, kimi.API_KEY = kimi.API_KEY, "KKEY"
        try:
            async with serve():
                out.append(await billing.report())
        finally:
            kimi.API_KEY = old_kimi
            unenv(saved_env)

    out = []
    defaults()
    MODE["yandex"] = {"code": 403, "json": {}}                      # ключ есть, прав на биллинг нет
    MODE["oa_credit"] = {"code": 500, "json": {}}
    MODE["oa_costs"] = {"code": 500, "json": {}}
    MODE["services"] = {"code": 500, "json": {}}
    asyncio.run(run(out))
    text = out[0]
    parts = text.split("\n" + billing.SEP + "\n")
    assert len(parts) == 4                                          # упавшие секции не ломают формат отчёта
    assert "• Доступно: 12,50" in parts[0]                          # Кими жив, хотя соседи упали
    assert "• нет доступа: ключу нужна роль billing.viewer" in parts[1]
    assert "• Кредиты: не получилось (см. лог)" in parts[2]
    assert "• Расход: не получилось (см. лог)" in parts[2]
    assert "• сервисы: не получилось (см. лог)" in parts[3]
    ok("403 → подсказка про billing.viewer; 500 → «не получилось»; живые секции целы")

    # мусор вместо JSON: та же ветка «не получилось», без исключений наверх
    defaults()
    MODE["yandex"] = {"raw": "не json"}
    asyncio.run(run(out))
    assert "• счета: не получилось (см. лог)" in out[-1].split("\n" + billing.SEP + "\n")[1]
    ok("мусор вместо JSON → «не получилось», отчёт не падает")

    # 401 → Billing API не принимает Api-Key: подсказка про OAuth-токен
    defaults()
    MODE["yandex"] = {"code": 401, "json": {}}
    asyncio.run(run(out))
    assert "• нет доступа: Billing API требует OAuth-токен" in out[-1].split("\n" + billing.SEP + "\n")[1]
    ok("401 → подсказка про YANDEX_OAUTH_TOKEN (Billing API не берёт Api-Key)")


def test_metrics_errors():
    """Упавшая метрика Render — «не получилось» только в своей строке; одна метрика не тянет остальные."""
    async def run():
        saved_env = env(RENDER_API_KEY="RKEY-555566667777", WHISPER_API_KEY="OKEY-9876543210")
        old_kimi, kimi.API_KEY = kimi.API_KEY, ""
        try:
            async with serve():
                return await billing.report()
        finally:
            kimi.API_KEY = old_kimi
            unenv(saved_env)

    defaults()
    for p in ("cpu-limit", "memory-limit", "disk-usage", "disk-capacity", "bandwidth"):
        MODE[p] = {"code": 500, "json": {}}
    MODE["oa_usage"] = {"code": 500, "json": {}}                     # половина секции OpenAI тоже падает
    text = asyncio.run(run())
    render = text.split("\n" + billing.SEP + "\n")[3]
    assert "• tutor-bot (srv-abc): план starter" in render           # список сервисов получен
    for label in ("CPU-лимит", "Память", "Диск", "Трафик с начала месяца"):
        assert f"  · {label}: не получилось" in render
    assert "• Даты списания: публичный API Render не отдаёт" in render
    openai = text.split("\n" + billing.SEP + "\n")[2]
    assert "• Кредиты: доступно 8,00 · использовано 10,00 из 18,00" in openai  # первая половина секции жива
    assert "• Расход: не получилось (см. лог)" in openai
    ok("упавшая метрика → «не получилось» в своей строке, остальные секции и строки целы")


if __name__ == "__main__":
    for t in (test_full_report, test_no_keys, test_errors, test_metrics_errors):
        t()
    print("OK")
