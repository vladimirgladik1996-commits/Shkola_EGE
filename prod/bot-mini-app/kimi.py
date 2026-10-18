"""Клиент Kimi API (Moonshot AI) — OpenAI-совместимый Chat Completions.

Подключение — переменными окружения (см. .env.example):
  KIMI_API_KEY    — ключ с https://platform.kimi.ai/ (без него ассистент отвечает заглушкой)
  KIMI_BASE_URL   — по умолчанию https://api.moonshot.ai/v1; для Китая — https://api.moonshot.cn/v1
  KIMI_MODEL      — модель «по умолчанию» (разбор задач), по умолчанию kimi-k2.6
  KIMI_MODEL_FAST — быстрые/дешёвые ответы на простые вопросы, по умолчанию kimi-k2.6
  KIMI_MODEL_DEEP — сильная модель для сложных вопросов, по умолчанию kimi-k3
  KIMI_EFFORT     — глубина размышления kimi-k3: low / high / max (по умолчанию max)
  KIMI_MAX_TOKENS — лимит обычного ответа в токенах, по умолчанию 800
  KIMI_MAX_TOKENS_DEEP — лимит «глубокого» ответа, по умолчанию 4000
  KIMI_TIMEOUT    — таймаут обычного запроса, по умолчанию 60
  KIMI_TIMEOUT_DEEP — таймаут «глубокого» запроса, по умолчанию 300

Важно: Kimi принимает текст, картинки и видео — ГОЛОС НЕ РАСПОЗНАЁТ (audio-входа нет):
распознаванием речи занимается отдельный STT (см. whisper.py).

Просьба «не падать» выполнена буквально: любая ошибка (нет ключа, сеть, таймаут,
неожиданный формат ответа) — это None из ask() и запись в лог, а не исключение наверх.
"""
import json, logging, os

import aiohttp

log = logging.getLogger("tutor.kimi")

BASE = (os.getenv("KIMI_BASE_URL", "").strip() or "https://api.moonshot.ai/v1").rstrip("/")  # пустое значение = дефолт
API_KEY = os.getenv("KIMI_API_KEY", "").strip()
MODEL = (os.getenv("KIMI_MODEL", "") or "kimi-k2.6").strip()
FAST_MODEL = (os.getenv("KIMI_MODEL_FAST", "") or "kimi-k2.6").strip()
DEEP_MODEL = (os.getenv("KIMI_MODEL_DEEP", "") or "kimi-k3").strip()
EFFORT = (os.getenv("KIMI_EFFORT", "") or "max").strip()
if EFFORT not in ("low", "high", "max"): EFFORT = "max"


def _int(name, default, lo, hi):
    try: v = int(os.getenv(name, "") or default)
    except ValueError: v = default
    return max(lo, min(hi, v))

def _flt(name, default, lo, hi):
    try: v = float(os.getenv(name, "") or default)
    except ValueError: v = default
    return max(lo, min(hi, v))

def _opt_int(name):
    v = os.getenv(name, "").strip()
    try:
        return int(v) if v else None
    except ValueError:
        return None

# ЛИМИТОВ НЕТ: по умолчанию max_tokens в запрос не передаётся вообще — модель отвечает
# сколько нужно (в пределах собственного максимума вывода модели, это не настраивается).
# Ограничить длину можно ТОЛЬКО явно — задав KIMI_MAX_TOKENS / KIMI_MAX_TOKENS_DEEP /
# KIMI_THINK_EXTRA в окружении. Пустые значения = без ограничений.
MAX_TOKENS = _opt_int("KIMI_MAX_TOKENS")
MAX_TOKENS_DEEP = _opt_int("KIMI_MAX_TOKENS_DEEP")
THINK_EXTRA = _opt_int("KIMI_THINK_EXTRA") or 0
TIMEOUT = _flt("KIMI_TIMEOUT", 60, 3, 600)
TIMEOUT_DEEP = _flt("KIMI_TIMEOUT_DEEP", 600, 3, 1800)


def enabled():
    """Есть ли ключ API. Без него ассистент работает заглушкой и наружу не ходит."""
    return bool(API_KEY)


def build_payload(messages, model=None, deep=False, json_mode=False, images=None, thinking=False, max_tokens=None):
    """Тело запроса /chat/completions.

    • temperature/top_p не передаём: у K2.x и K3 они фиксированы, API отвечает ошибкой
      на любые другие значения.
    • thinking — параметр только семейства K2.x; по умолчанию выключен (быстрые ответы),
      thinking=True включает размышление (reasoning_content) — оно стримится в ask_stream.
    • reasoning_effort — только kimi-k3 («глубокое изучение»); K3 думает всегда.
    • json_mode — ответ строго в JSON (response_format json_object): используется для разбора задач.
    • images — список base64/jpeg|png|webp: подмешиваются в последнее сообщение (vision)."""
    m = (model or (DEEP_MODEL if deep else MODEL)).strip()
    content = messages[-1]["content"]
    if images:
        parts = [{"type": "text", "text": content if isinstance(content, str) else ""}]
        for i in images:
            parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + i}})
        content = parts
    msgs = list(messages[:-1]) + [{**messages[-1], "content": content}]
    # Лимит передаётся только если его ЯВНО задали (env или повтор запроса): без него
    # max_tokens не отправляется вовсе и длина ответа ничем не ограничена с нашей стороны.
    reasoning = thinking or m.startswith("kimi-k3")
    budget = max_tokens if max_tokens is not None else \
        ((MAX_TOKENS_DEEP if deep else MAX_TOKENS) or 0) + (THINK_EXTRA if reasoning else 0)
    p = {"model": m, "messages": msgs}
    if budget:
        p["max_tokens"] = budget
    if m.startswith("kimi-k2.6"):
        p["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if deep and m.startswith("kimi-k3"):
        p["reasoning_effort"] = EFFORT
    if json_mode:
        p["response_format"] = {"type": "json_object"}
    return p


async def ask(messages, model=None, deep=False, json_mode=False, images=None, max_tokens=None, _attempt=0):
    """Ответ модели (str) или None — нет ключа / API недоступен / формат ответа неожиданный."""
    if not API_KEY:
        return None
    body = build_payload(messages, model=model, deep=deep, json_mode=json_mode, images=images,
                         max_tokens=max_tokens)
    timeout = TIMEOUT_DEEP if deep else TIMEOUT
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.post(BASE + "/chat/completions", json=body,
                              headers={"Authorization": "Bearer " + API_KEY}) as r:
                raw = await r.text()
                if r.status != 200:
                    log.warning("kimi: HTTP %s: %s", r.status, raw[:300])
                    if r.status == 400 and "max_tokens" in raw and _attempt == 0:
                        # API не принял лимит токенов — повторяем с безопасным
                        return await ask(messages, model=model, deep=deep, json_mode=json_mode,
                                         images=images, max_tokens=0, _attempt=_attempt + 1)
                    return None
        j = json.loads(raw)
        ch = (j.get("choices") or [{}])[0]
        reply = (ch.get("message") or {}).get("content")
        if not isinstance(reply, str) or not reply.strip():
            # частая причина: бюджет max_tokens сгорел на размышлениях (finish_reason=length)
            log.warning("kimi: пустой ответ (finish=%s usage=%s): %s",
                        ch.get("finish_reason"), j.get("usage"), raw[:300])
            return None
        return reply.strip()
    except Exception as e:                      # сеть, таймаут, кривой JSON — всё мимо бота
        log.warning("kimi: запрос не прошёл: %r", e)
        return None


async def ask_stream(messages, model=None, deep=False, json_mode=False, images=None, thinking=True,
                     max_tokens=None, _attempt=0):
    """Стриминг ответа модели: async-генератор пар (тип, дельта).

    Типы: «think» — кусочек размышления (reasoning_content), «text» — кусочек ответа,
    «err» — сбой («off» — нет ключа, «http» — API отказал, «net» — сеть/таймаут,
    «empty» — стрим дошёл до конца без единой дельты ответа); после err генератор завершается.
    Мысль идёт отдельными дельтами — её показывают «от имени Вишенки» («Вишенка думает…»).
    «Пустой» стрим (бюджет токенов сгорел на размышлениях) автоматически повторяется один раз
    с удвоенным лимитом max_tokens — иначе пользователь видит «ИИ не отвечает» без причины."""
    if not API_KEY:
        yield "err", "off"
        return
    body = build_payload(messages, model=model, deep=deep, json_mode=json_mode, images=images,
                         thinking=thinking, max_tokens=max_tokens)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}    # usage в конце стрима — видно, сгорел ли бюджет
    timeout = TIMEOUT_DEEP if deep else TIMEOUT
    finish, usage, got_text = None, None, False
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.post(BASE + "/chat/completions", json=body,
                              headers={"Authorization": "Bearer " + API_KEY}) as r:
                if r.status != 200:
                    raw = await r.text()
                    log.warning("kimi: HTTP %s: %s", r.status, raw[:300])
                    if r.status == 400 and "max_tokens" in raw and _attempt == 0:
                        async for item in ask_stream(messages, model=model, deep=deep, json_mode=json_mode,
                                                     images=images, thinking=thinking,
                                                     max_tokens=0, _attempt=_attempt + 1):
                            yield item
                    else:
                        yield "err", "http"
                    return
                async for line in r.content:
                    s_line = line.decode("utf-8", "replace").strip()
                    if not s_line.startswith("data:"):
                        continue
                    data = s_line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        j = json.loads(data)
                    except ValueError:
                        continue
                    if j.get("usage"):
                        usage = j["usage"]
                    ch = (j.get("choices") or [{}])[0]
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    d = ch.get("delta") or {}
                    th, tx = d.get("reasoning_content"), d.get("content")
                    if th:
                        yield "think", th
                    if tx:
                        got_text = True
                        yield "text", tx
    except Exception as e:                      # сеть, таймаут, кривой SSE — всё мимо бота
        log.warning("kimi: стрим не прошёл: %r", e)
        yield "err", "net"
        return
    if not got_text:
        # стрим дошёл до конца, но контента нет: почти всегда бюджет max_tokens
        # сгорел на размышлениях (finish_reason=length) — повторяем с большим лимитом
        log.warning("kimi: стрим без ответа (finish=%s usage=%s max_tokens=%s)",
                    finish, usage, body.get("max_tokens"))
        if _attempt == 0:
            # повтор без какого-либо лимита: пусть модель дописывает сколько нужно
            async for item in ask_stream(messages, model=model, deep=deep, json_mode=json_mode,
                                         images=images, thinking=thinking,
                                         max_tokens=0, _attempt=_attempt + 1):
                yield item
        else:
            yield "err", "empty"


async def balance():
    """Баланс аккаунта Moonshot/Kimi (dict с available_balance/voucher_balance/cash_balance) или None.

    Деньги считаются в юанях (CNY) — как в личном кабинете platform.kimi.ai.
    Использует тот же KIMI_API_KEY, что и все запросы модели."""
    if not API_KEY:
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as s:
            async with s.get(BASE + "/users/me/balance",
                             headers={"Authorization": "Bearer " + API_KEY}) as r:
                raw = await r.text()
                if r.status != 200:
                    log.warning("kimi: balance HTTP %s: %s", r.status, raw[:300])
                    return None
        j = json.loads(raw)
        d = j.get("data") if isinstance(j, dict) else None
        return d if isinstance(d, dict) else None
    except Exception as e:
        log.warning("kimi: баланс не получился: %r", e)
        return None


async def ask_json(messages, **kw):
    """Ответ как dict (модель отвечает строгим JSON) или None. Просит JSON через response_format
    и дополнительно проверяет разбор — кривой JSON не должен ронять конвейер."""
    raw = await ask(messages, json_mode=True, **kw)
    if raw is None:
        return None
    try:
        j = json.loads(raw)
    except ValueError:
        log.warning("kimi: ответ не разобрался как JSON: %s", raw[:300])
        return None
    return j if isinstance(j, dict) else None
