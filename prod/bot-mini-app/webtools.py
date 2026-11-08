"""Официальные инструменты Kimi (Formula API) — модель сама ходит в сеть через tool_calls.

С 20.10.2026 старый встроенный $web_search Kimi выведен из эксплуатации: поиск и загрузка
страниц больше не встроены в Chat Completions, а вызываются как инструменты:
  1. GET  {KIMI_BASE_URL}/formulas/{uri}/tools   — декларации инструментов (кэшируются);
  2. POST {KIMI_BASE_URL}/formulas/{uri}/fibers  — исполнение tool_call (name+arguments как есть).
Подробности: https://platform.kimi.ai/docs/guide/use-official-tools.md
Веб-поиск тарифицируется за вызов (WebSearch Pricing), fetch — бесплатный (ограниченное время).

Роли: МОДЕЛЬ решает, когда искать (сама отдаёт tool_calls на web_search/fetch), ЭТОТ модуль
только исполняет их и возвращает результат в историю (role=tool). Подключается в «Чате с ИИ»
(assist.chat_stream) через kimi.ask_stream(tools=…, tool_exec=…).

Включается флагом KIMI_WEBTOOLS=1 (см. .env.example) при заданном KIMI_API_KEY. Без флага чат
работает как раньше — без инструментов. Любой сбой (нет сети, отказ API) — это None или строка
«инструмент недоступен», исключение наружу не выходит: чат просто отвечает без поиска.
"""
import json, logging, os, time

import aiohttp

import kimi

log = logging.getLogger("tutor.webtools")

# Официальные формулы Kimi: веб-поиск и загрузка страницы по URL.
FORMULAS = ("moonshot/web-search:latest", "moonshot/fetch:latest")
DECL_TTL = 3600.0        # декларации инструментов почти не меняются — кэш на час
RETRY_ON_FAIL = 300.0    # после сбоя повторной загрузки не раньше чем через 5 минут
FIBER_TIMEOUT = 120.0    # один вызов инструмента (поиск или загрузка страницы)
RESULT_MAX = 20000       # столько символов результата уходит в историю (как в chat_stream)

_on = (os.getenv("KIMI_WEBTOOLS", "").strip().lower() not in ("", "0", "off", "false", "no"))
_cache = {"t": None, "tools": None, "by_name": {}}


def enabled():
    """Флаг KIMI_WEBTOOLS включён И ключ API есть — только тогда чат получает инструменты."""
    return _on and kimi.enabled()


def _tool_name(t):
    """Имя инструмента из декларации (формат OpenAI или плоский) — или None."""
    if not isinstance(t, dict):
        return None
    fn = t.get("function") if isinstance(t.get("function"), dict) else {}
    return fn.get("name") or t.get("name")


async def _fetch_all(s):
    """Загрузить декларации всех формул: ([декларации…], {имя: uri}). Ошибка — исключение."""
    tools, by_name = [], {}
    for uri in FORMULAS:
        async with s.get(kimi.BASE + "/formulas/%s/tools" % uri,
                         headers={"Authorization": "Bearer " + kimi.API_KEY}) as r:
            raw = await r.text()
            if r.status != 200:
                raise RuntimeError("HTTP %s: %s" % (r.status, raw[:200]))
            j = json.loads(raw)
        for t in (j.get("tools") or []):
            name = _tool_name(t)
            if name:
                by_name[name] = uri
                tools.append(t)
    return tools, by_name


async def declarations():
    """Декларации инструментов для поля tools чат-запроса: list[dict] или None.

    Кэш живёт DECL_TTL; после сбоя повтор не раньше RETRY_ON_FAIL. Сбой при обновлении
    не сбрасывает рабочий кэш — остаёмся со старыми декларациями (stale-while-revalidate).
    None — чат работает без инструментов (как до их появления)."""
    if not enabled():
        return None
    now = time.monotonic()
    if _cache["tools"] and _cache["t"] is not None and now - _cache["t"] < DECL_TTL:
        return _cache["tools"]                     # свежий кэш — без похода в сеть
    if _cache["t"] is not None and now - _cache["t"] < RETRY_ON_FAIL:
        return _cache["tools"]                     # только что пробовали: кэш или None
    _cache["t"] = now
    try:
        async with aiohttp.ClientSession(timeout=kimi.request_timeout(30)) as s:
            tools, by_name = await _fetch_all(s)
    except Exception as e:
        log.warning("webtools: декларации не загрузились: %r", e)
        return _cache["tools"]                     # сбой: старый кэш или None, ничего не падает
    if not tools:
        return _cache["tools"]
    _cache["tools"], _cache["by_name"] = tools, by_name
    log.info("webtools: подключены инструменты %s", sorted(by_name))
    return tools


def _result_text(j):
    """Результат fibers → строка для role=tool: output, иначе encrypted_output (его модель читает)."""
    ctx = j.get("context") if isinstance(j.get("context"), dict) else {}
    out = ctx.get("output") or ctx.get("encrypted_output") or j.get("output")
    if out is None:
        return ""
    return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


async def run(name, arguments):
    """Исполнение одного tool_call модели: имя + аргументы передаются в fibers как есть.

    Возвращает строку-результат для role=tool; НИКОГДА не бросает исключение —
    любой сбой превращается в короткую строку, и модель отвечает без инструмента."""
    uri = _cache["by_name"].get(name) or _guess_uri(name)
    if not uri:
        return "Инструмент %s недоступен — ответь без него." % name
    body = {"name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)}
    try:
        async with aiohttp.ClientSession(timeout=kimi.request_timeout(FIBER_TIMEOUT)) as s:
            async with s.post(kimi.BASE + "/formulas/%s/fibers" % uri, json=body,
                              headers={"Authorization": "Bearer " + kimi.API_KEY}) as r:
                raw = await r.text()
                if r.status != 200:
                    log.warning("webtools: fibers HTTP %s: %s", r.status, raw[:300])
                    return "Инструмент %s не сработал (HTTP %s) — ответь без него." % (name, r.status)
    except Exception as e:
        log.warning("webtools: fibers не прошёл: %r", e)
        return "Инструмент %s недоступен — ответь без него." % name
    try:
        j = json.loads(raw)
    except ValueError:
        log.warning("webtools: не JSON в ответе fibers: %s", raw[:300])
        return "Инструмент %s не сработал — ответь без него." % name
    text = _result_text(j).strip()
    if not text:
        return "Инструмент %s не вернул результат — ответь без него." % name
    log.info("webtools: %s → %d символов", name, len(text))
    return text[:RESULT_MAX]


def _guess_uri(name):
    """Имя инструмента → uri формулы, если декларации ещё не грузились (web_search/fetch)."""
    return {"web_search": FORMULAS[0], "search": FORMULAS[0], "fetch": FORMULAS[1]}.get(name)
