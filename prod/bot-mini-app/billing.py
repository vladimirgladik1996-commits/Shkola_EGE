"""Сводный отчёт по счетам — /balance: одно сообщение, две секции — Кими и Яндекс Cloud.

Зачем: деньги лежат в двух местах (Kimi и Яндекс Cloud) — сводка в одном сообщении
показывает всё сразу, без похода в два кабинета. Балансы Render и OpenAI убраны:
в /balance остаются только Кими и Яндекс.

Ключи — из окружения (см. render.yaml). Сами значения ключей в логи и ответы
не попадают ни при каких условиях.

Секция каждого провайдера живёт своей жизнью: пустой ключ или упавший запрос
одного не ломает остальные — в секции просто появляется «не получилось».

Переменные окружения:
  KIMI_API_KEY             — см. kimi.py (секция Кими переиспользует kimi.balance())
  YANDEX_SA_KEY            — JSON авторизованного ключа сервисного аккаунта (содержимое
                             key.json целиком): подписывается JWT (PS256) и обменивается
                             на IAM-токен — так Billing API работает по документации
                             (Authorization: Bearer). Сервисному аккаунту нужна роль
                             billing.accounts.viewer (billing.viewer) на платёжном аккаунте
  YANDEX_SA_KEY_FILE       — то же самое, но путь к файлу с JSON-ключом (если так удобнее)
  YANDEX_OAUTH_TOKEN       — запасной способ: OAuth-токен Яндекса (yandex.ru/oauth) —
                             тоже принимается (Authorization: OAuth)
  YANDEX_BILLING_API_KEY   — последний резерв: Api-Key (если пусто, берётся YANDEX_API_KEY) —
                             Billing API его отвергает с 401, оставлен для диагностики
  YANDEX_BILLING_ID        — id платёжного аккаунта (dn2…): с ним баланс берётся точечно методом
                             BillingAccount.Get (billing/v1/billingAccounts/{id}); без него — список всех
                             счетов методом BillingAccount.List
"""
import asyncio, json, logging, os, re, time

import aiohttp

import kimi

log = logging.getLogger("tutor.billing")

TIMEOUT = 15            # каждый запрос — не дольше 15 с: зависший провайдер не тянет весь отчёт
SEP = "______"          # разделитель секций в одном сообщении

# Адреса API вынесены в константы: тесты подменяют их фейковым сервером (см. tests/test_billing.py)
YANDEX_URL = "https://billing.api.cloud.yandex.net/billing/v1/billingAccounts"
IAM_URL = "https://iam.api.cloud.yandex.net/iam/v1/tokens"   # обмен JWT сервисного аккаунта на IAM-токен


def _key(*names):
    """Первый непустой ключ из списка переменных окружения (второе имя — запасное)."""
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return ""


_iam_cache = {"token": "", "until": 0.0}   # IAM-токен действует часами — кэшируем на полчаса


def _sa_key():
    """JSON авторизованного ключа сервисного аккаунта (YANDEX_SA_KEY или YANDEX_SA_KEY_FILE).

    None — ключа нет или он не читается: у секции есть запасные способы авторизации."""
    raw = os.getenv("YANDEX_SA_KEY", "").strip()
    if not raw:
        path = os.getenv("YANDEX_SA_KEY_FILE", "").strip()
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    raw = f.read()
            except OSError as e:
                log.warning("billing: YANDEX_SA_KEY_FILE не прочитался: %r", e)
    if not raw:
        return None
    try:
        key = json.loads(raw)
    except ValueError as e:
        log.warning("billing: YANDEX_SA_KEY — не JSON: %r", e)
        return None
    if not isinstance(key, dict) or not key.get("private_key") or not key.get("service_account_id"):
        log.warning("billing: в YANDEX_SA_KEY нет private_key/service_account_id")
        return None
    return key


async def _iam(s):
    """IAM-токен для Billing API: JWT (PS256, ключом сервисного аккаунта) → Bearer.

    Как в документации Яндекса (iam/operations/iam-token/create-for-sa): alg=PS256 + kid,
    iss — сервисный аккаунт, aud — адрес токен-сервиса, exp−iat ≤ 3600 с.
    Токен кэшируется на полчаса: каждый /balance не запрашивает новый."""
    key = _sa_key()
    if not key:
        return ""
    now = time.time()
    if _iam_cache["token"] and now < _iam_cache["until"]:
        return _iam_cache["token"]
    try:
        import jwt  # PyJWT + cryptography (PS256) — в requirements.txt
        pem = "\n".join(l for l in str(key["private_key"]).splitlines()
                        if "PLEASE DO NOT REMOVE" not in l)   # служебная строка Яндекса рядом с PEM
        block = re.search(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", pem, re.S)
        assertion = jwt.encode(
            {"iss": key["service_account_id"], "aud": IAM_URL,
             "iat": int(now), "exp": int(now) + 3600},
            block.group(0) if block else pem, algorithm="PS256",
            headers={"typ": "JWT", "kid": str(key.get("id", ""))})
    except Exception as e:
        log.warning("billing: JWT из YANDEX_SA_KEY не подписался: %r", e)
        return ""
    try:
        async with s.post(IAM_URL, json={"jwt": assertion}) as r:
            if r.status != 200:
                log.warning("billing: IAM-токен не выдался (HTTP %s)", r.status)
                return ""
            j = await r.json(content_type=None)
    except Exception as e:
        log.warning("billing: IAM-токен не выдался: %r", e)
        return ""
    token = j.get("iamToken") if isinstance(j, dict) else ""
    if token:
        _iam_cache.update(token=token, until=now + 1800)
    return token or ""


def _money(x):
    """Деньги как в старом /balance: 1234.5 → «1 234,50», не-число → «—»."""
    try: return f"{float(x):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError): return "—"


def _num(x):
    """Компактное число для лимитов: 500 → «500», 0.5 → «0,5», не-число → «—»."""
    try: return (f"{float(x):,.2f}".rstrip("0").rstrip(".") or "0").replace(",", " ").replace(".", ",")
    except (TypeError, ValueError): return "—"


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

    Авторизация по документации — IAM-токен (Bearer) из JSON-ключа сервисного аккаунта
    (YANDEX_SA_KEY); токен из YANDEX_OAUTH_TOKEN принимается в обоих видах: IAM-токен (формат
    y0__/t1.) уходит как Bearer, OAuth-токен (y0_Ag…) — как OAuth; Api-Key отвергается с 401 — проверено."""
    token = await _iam(s)
    if token:
        auth = "Bearer " + token
    else:
        oauth = _key("YANDEX_OAUTH_TOKEN")
        api_key = _key("YANDEX_BILLING_API_KEY", "YANDEX_API_KEY")
        if not oauth and not api_key:
            return ("☁️ Яндекс Cloud:\n• ключ не задан (YANDEX_SA_KEY / "
                    "YANDEX_OAUTH_TOKEN / YANDEX_API_KEY)")
        if oauth and (oauth.startswith("y0__") or oauth.startswith("t1.")):
            auth = "Bearer " + oauth   # это IAM-токен, а не OAuth: схема Bearer — иначе Яндекс отвечает 401
        else:
            auth = "OAuth " + oauth if oauth else "Api-Key " + api_key
    bid = _key("YANDEX_BILLING_ID")   # id платёжного аккаунта → точечный BillingAccount.Get
    try:
        async with s.get(YANDEX_URL + ("/" + bid if bid else ""), headers={"Authorization": auth}) as r:
            if r.status == 401:
                return ("☁️ Яндекс Cloud:\n• нет доступа: ключ не принят — задайте "
                        "YANDEX_SA_KEY (JSON-ключ сервисного аккаунта)")
            if r.status == 403:
                # чаще всего ключ есть, но у сервисного аккаунта нет прав на биллинг
                return ("☁️ Яндекс Cloud:\n• нет доступа: ключу нужна роль "
                        "billing.accounts.viewer (billing.viewer)")
            if r.status == 404 and bid:
                return f"☁️ Яндекс Cloud:\n• счёт {bid} не найден (проверьте YANDEX_BILLING_ID)"
            if r.status != 200:
                log.warning("billing: Яндекс HTTP %s", r.status)
                return f"☁️ Яндекс Cloud:\n• счета: не получилось (HTTP {r.status})"
            j = await r.json(content_type=None)
    except Exception as e:
        log.warning("billing: Яндекс не ответил: %r", e)
        return "☁️ Яндекс Cloud:\n• счета: не получилось (см. лог)"
    if bid:                              # BillingAccount.Get: ответ — сам счёт, а не список
        accounts = [j] if isinstance(j, dict) and j.get("id") else None
    else:
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


async def report():
    """Сводка по счетам одним сообщением; секции собираются параллельно.

    Секции разделяются строкой «______». Ошибка одной секции не ломает остальные —
    заголовок и «не получилось» у падшей, остальные как есть."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as s:
        secs = await asyncio.gather(_kimi(), _yandex(s), return_exceptions=True)
    titles = ["💳 Кими (Moonshot), CNY:", "☁️ Яндекс Cloud:"]
    out = []
    for title, sec in zip(titles, secs):
        if isinstance(sec, BaseException):               # страховка: сбой внутри секции — не всего отчёта
            log.warning("billing: секция «%s» упала: %r", title, sec)
            sec = f"{title}\n• не получилось (см. лог)"
        out.append(sec)
    return ("\n" + SEP + "\n").join(out)
