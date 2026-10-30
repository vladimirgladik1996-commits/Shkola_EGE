"""Сводный отчёт по счетам всех провайдеров — /balance: одно сообщение, четыре секции.

Зачем: деньги лежат в четырёх местах (Kimi, Яндекс Cloud, OpenRouter, Render) —
сводка в одном сообщении показывает всё сразу, без похода в четыре кабинета.

Ключи — из окружения (см. render.yaml). Сами значения ключей в логи и ответы
не попадают ни при каких условиях.

Секция каждого провайдера живёт своей жизнью: пустой ключ или упавший запрос
одного не ломает остальные — в секции просто появляется «не получилось».

Переменные окружения:
  KIMI_API_KEY             — см. kimi.py (секция Кими переиспользует kimi.balance())
  YANDEX_OAUTH_TOKEN       — OAuth-токен Яндекса (yandex.ru/oauth): Billing API понимает
                             ТОЛЬКО его, обычный Api-Key отвергает с 401
  YANDEX_BILLING_API_KEY   — запасной вариант: Api-Key с ролью billing.viewer
                             (если пусто, берётся YANDEX_API_KEY — но биллинг его, скорее всего, не примет)
  OPENROUTER_API_KEY       — ключ https://openrouter.ai/settings/keys
  RENDER_API_KEY           — ключ Render: Dashboard → Account Settings → API Keys

Дата списания денег у Render публичным API не отдаётся — так и написано в секции.
"""
import asyncio, logging, os
from datetime import datetime, timezone

import aiohttp

import kimi

log = logging.getLogger("tutor.billing")

TIMEOUT = 15            # каждый запрос — не дольше 15 с: зависший провайдер не тянет весь отчёт
SEP = "______"          # разделитель секций в одном сообщении

# Адреса API вынесены в константы: тесты подменяют их фейковым сервером (см. tests/test_billing.py)
YANDEX_URL = "https://billing.api.cloud.yandex.net/billing/v1/billingAccounts"
OR_CREDITS = "https://openrouter.ai/api/v1/credits"
OR_KEY = "https://openrouter.ai/api/v1/auth/key"
RENDER_URL = "https://api.render.com/v1"


def _key(*names):
    """Первый непустой ключ из списка переменных окружения (второе имя — запасное)."""
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return ""


def _money(x):
    """Деньги как в старом /balance: 1234.5 → «1 234,50», не-число → «—»."""
    try: return f"{float(x):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError): return "—"


def _num(x):
    """Компактное число для лимитов: 500 → «500», 0.5 → «0,5», не-число → «—»."""
    try: return (f"{float(x):,.2f}".rstrip("0").rstrip(".") or "0").replace(",", " ").replace(".", ",")
    except (TypeError, ValueError): return "—"


def _metric(payload):
    """(значение, ед. изм.) последней точки таймсерии Render или (None, "").

    Render отдаёт массив таймсерий [{labels, values, unit}], а значения бывают
    и просто числами, и объектами {startTime|timestamp, value} — приемлем оба
    варианта и берём последнюю точку (для счётчиков вроде трафика это итог периода)."""
    value, unit = None, ""
    for series in payload if isinstance(payload, list) else []:
        if not isinstance(series, dict):
            continue
        unit = unit or str(series.get("unit") or "")
        vals = series.get("values")
        if isinstance(vals, list) and vals:
            last = vals[-1]
            value = last.get("value") if isinstance(last, dict) else last
    return value, unit


async def _kimi():
    """Секция Кими: те же поля, что и в старом /balance (деньги в юанях)."""
    lines = ["💳 Кими (Moonshot), CNY:"]
    if not kimi.API_KEY:
        lines.append("• ключ не задан (KIMI_API_KEY)")
        return "\n".join(lines)
    try:
        # у kimi.balance() свой долгий таймаут — здесь держим общий потолок 15 с
        b = await asyncio.wait_for(kimi.balance(), TIMEOUT)
    except Exception as e:
        log.warning("billing: Кими не ответил: %r", e)
        b = None
    if not isinstance(b, dict):
        lines.append("• баланс: не получилось (см. лог)")
        return "\n".join(lines)
    lines += [
        f"• Доступно: {_money(b.get('available_balance'))}",
        f"• Ваучеры: {_money(b.get('voucher_balance'))}",
        f"• Основной счёт: {_money(b.get('cash_balance'))}",
    ]
    return "\n".join(lines)


async def _yandex(s):
    """Секция Яндекс Cloud: баланс, валюта и статус каждого счёта.

    Billing API принимает только OAuth-токены: на Api-Key отвечает 401
    «invalid authorization metadata» даже с ролью billing.viewer — проверено."""
    oauth = _key("YANDEX_OAUTH_TOKEN")
    api_key = _key("YANDEX_BILLING_API_KEY", "YANDEX_API_KEY")
    if not oauth and not api_key:
        return "☁️ Яндекс Cloud:\n• ключ не задан (YANDEX_OAUTH_TOKEN / YANDEX_API_KEY)"
    auth = "OAuth " + oauth if oauth else "Api-Key " + api_key
    try:
        async with s.get(YANDEX_URL, headers={"Authorization": auth}) as r:
            if r.status == 401:
                return ("☁️ Яндекс Cloud:\n• нет доступа: Billing API требует OAuth-токен — "
                        "задайте YANDEX_OAUTH_TOKEN (yandex.ru/oauth)")
            if r.status == 403:
                # чаще всего ключ есть, но у него нет прав на биллинг
                return "☁️ Яндекс Cloud:\n• нет доступа: ключу нужна роль billing.viewer"
            if r.status != 200:
                log.warning("billing: Яндекс HTTP %s", r.status)
                return f"☁️ Яндекс Cloud:\n• счета: не получилось (HTTP {r.status})"
            j = await r.json(content_type=None)
    except Exception as e:
        log.warning("billing: Яндекс не ответил: %r", e)
        return "☁️ Яндекс Cloud:\n• счета: не получилось (см. лог)"
    accounts = j.get("billingAccounts") if isinstance(j, dict) else None
    if not accounts:
        return "☁️ Яндекс Cloud:\n• счета не найдены"
    curr = str(accounts[0].get("currency") or "—")       # валюта в заголовке — по первому счёту
    lines = [f"☁️ Яндекс Cloud, {curr}:"]
    for a in accounts:
        if not isinstance(a, dict):
            continue
        name = a.get("name") or a.get("id") or "?"
        st = a.get("status") or ("активен" if a.get("active") else "заблокирован")
        extra = f" {a.get('currency')}" if a.get("currency") and a.get("currency") != curr else ""
        lines.append(f"• {name} ({a.get('id')}): {_money(a.get('balance'))}{extra} · {st}")
    return "\n".join(lines)


async def _openrouter(s):
    """Секция OpenRouter: кредиты аккаунта и лимит отдельного ключа (USD)."""
    key = _key("OPENROUTER_API_KEY")
    lines = ["🌐 OpenRouter, USD:"]
    if not key:
        lines.append("• ключ не задан (OPENROUTER_API_KEY)")
        return "\n".join(lines)
    h = {"Authorization": "Bearer " + key}
    try:
        async with s.get(OR_CREDITS, headers=h) as r:
            if r.status != 200:
                raise RuntimeError(f"credits HTTP {r.status}")
            d = (await r.json(content_type=None) or {}).get("data") or {}
        total, used = d.get("total_credits"), d.get("total_usage")
        rest = float(total) - float(used)                # остаток считаем сами: API отдаёт только пару
        lines.append(f"• Кредиты: {_money(total)} · использовано: {_money(used)}")
        lines.append(f"• Остаток: {_money(rest)}")
    except Exception as e:
        log.warning("billing: OpenRouter (кредиты) не ответил: %r", e)
        lines.append("• Кредиты: не получилось (см. лог)")
    try:
        async with s.get(OR_KEY, headers=h) as r:
            if r.status != 200:
                raise RuntimeError(f"auth/key HTTP {r.status}")
            d = (await r.json(content_type=None) or {}).get("data") or {}
        lim = d.get("limit")
        lim_txt = "без лимита" if lim is None else _money(lim)   # limit=null — ключ без потолка
        free = "да" if d.get("is_free_tier") else "нет"
        lines.append(f"• Лимит ключа: {lim_txt} · использовано: {_money(d.get('usage'))}")
        lines.append(f"• Бесплатный тариф: {free}")
    except Exception as e:
        log.warning("billing: OpenRouter (ключ) не ответил: %r", e)
        lines.append("• Лимит ключа: не получилось (см. лог)")
    return "\n".join(lines)


def _plan(svc):
    """План сервиса (у разных типов сервисов поле лежит в разных объектах) или «—»."""
    d = svc.get("serviceDetails") or svc.get("details")
    if isinstance(d, dict) and d.get("plan"):
        return f"план {d['plan']}"
    return "план —"


async def _limits(s, h, rid):
    """Лимиты и использование одного сервиса Render; упавший запрос — «не получилось» в своей строке."""
    month = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    now = datetime.now(timezone.utc)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    async def get(path, params):
        # пути — как в публичном OpenAPI Render (metrics/cpu-limit и т.п.), фильтр — ?resource=<id>
        try:
            async with s.get(f"{RENDER_URL}/{path}", headers=h, params={"resource": rid, **params}) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                return _metric(await r.json(content_type=None))
        except Exception as e:
            log.warning("billing: Render %s (%s) не ответил: %r", path, rid, e)
            return None, ""

    cpu, cpu_u = await get("metrics/cpu-limit", {})
    mem, mem_u = await get("metrics/memory-limit", {})
    disk, disk_u = await get("metrics/disk-usage", {})
    cap, cap_u = await get("metrics/disk-capacity", {})
    # трафик — с начала месяца (UTC) по сейчас; последняя точка счётчика = итог периода
    bw, bw_u = await get("metrics/bandwidth", {"startTime": iso(month), "endTime": iso(now)})

    def line(label, value, unit):
        return f"  · {label}: " + ("не получилось" if value is None else f"{_num(value)} {unit}".rstrip())

    return [
        line("CPU-лимит", cpu, cpu_u),
        line("Память", mem, mem_u),
        "  · Диск: " + ("не получилось" if disk is None or cap is None
                        else f"{_num(disk)} {disk_u} из {_num(cap)} {cap_u}".rstrip()),
        line("Трафик с начала месяца", bw, bw_u),
    ]


async def _render(s):
    """Секция Render: сервисы, их планы и лимиты ресурсов."""
    key = _key("RENDER_API_KEY")
    lines = ["🖥 Render, USD:"]
    if not key:
        lines.append("• ключ не задан (RENDER_API_KEY)")
        return "\n".join(lines)
    h = {"Authorization": "Bearer " + key}
    try:
        async with s.get(RENDER_URL + "/services", headers=h, params={"limit": 50}) as r:
            if r.status != 200:
                raise RuntimeError(f"services HTTP {r.status}")
            j = await r.json(content_type=None)
    except Exception as e:
        log.warning("billing: Render (сервисы) не ответил: %r", e)
        lines.append("• сервисы: не получилось (см. лог)")
        return "\n".join(lines)
    services = []
    for item in j if isinstance(j, list) else []:
        svc = item.get("service") if isinstance(item, dict) else None      # список приходит обёртками {service, cursor}
        if isinstance(svc, dict):
            services.append(svc)
        elif isinstance(item, dict) and item.get("id"):
            services.append(item)                                          # на случай прямого списка сервисов
    if not services:
        lines.append("• сервисы не найдены")
    for svc in services:
        rid = svc.get("id")
        lines.append(f"• {svc.get('name') or '?'} ({rid}): {_plan(svc)}")
        lines += await _limits(s, h, rid)
    lines.append("• Даты списания: публичный API Render не отдаёт")
    return "\n".join(lines)


async def report():
    """Сводка по всем провайдерам одним сообщением; секции собираются параллельно.

    Секции разделяются строкой «______». Ошибка одной секции не ломает остальные —
    заголовок и «не получилось» у падшей, остальные как есть."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as s:
        secs = await asyncio.gather(_kimi(), _yandex(s), _openrouter(s), _render(s), return_exceptions=True)
    titles = ["💳 Кими (Moonshot), CNY:", "☁️ Яндекс Cloud:", "🌐 OpenRouter, USD:", "🖥 Render, USD:"]
    out = []
    for title, sec in zip(titles, secs):
        if isinstance(sec, BaseException):               # страховка: сбой внутри секции — не всего отчёта
            log.warning("billing: секция «%s» упала: %r", title, sec)
            sec = f"{title}\n• не получилось (см. лог)"
        out.append(sec)
    return ("\n" + SEP + "\n").join(out)
