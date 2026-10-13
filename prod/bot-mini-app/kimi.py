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

MAX_TOKENS = _int("KIMI_MAX_TOKENS", 800, 64, 8192)
MAX_TOKENS_DEEP = _int("KIMI_MAX_TOKENS_DEEP", 4000, 64, 16384)
TIMEOUT = _flt("KIMI_TIMEOUT", 60, 3, 600)
TIMEOUT_DEEP = _flt("KIMI_TIMEOUT_DEEP", 300, 3, 900)


def enabled():
    """Есть ли ключ API. Без него ассистент работает заглушкой и наружу не ходит."""
    return bool(API_KEY)


def build_payload(messages, model=None, deep=False, json_mode=False, images=None):
    """Тело запроса /chat/completions.

    • temperature/top_p не передаём: у K2.x и K3 они фиксированы, API отвечает ошибкой
      на любые другие значения.
    • thinking — параметр только семейства K2.x; по умолчанию выключен (быстрые ответы).
    • reasoning_effort — только kimi-k3 («глубокое изучение»).
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
    p = {"model": m, "messages": msgs, "max_tokens": MAX_TOKENS_DEEP if deep else MAX_TOKENS}
    if m.startswith("kimi-k2.6"):
        p["thinking"] = {"type": "disabled"}
    if deep and m.startswith("kimi-k3"):
        p["reasoning_effort"] = EFFORT
    if json_mode:
        p["response_format"] = {"type": "json_object"}
    return p


async def ask(messages, model=None, deep=False, json_mode=False, images=None):
    """Ответ модели (str) или None — нет ключа / API недоступен / формат ответа неожиданный."""
    if not API_KEY:
        return None
    body = build_payload(messages, model=model, deep=deep, json_mode=json_mode, images=images)
    timeout = TIMEOUT_DEEP if deep else TIMEOUT
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.post(BASE + "/chat/completions", json=body,
                              headers={"Authorization": "Bearer" + " " + API_KEY}) as r:
                raw = await r.text()
                if r.status != 200:
                    log.warning("kimi: HTTP %s: %s", r.status, raw[:300])
                    return None
        j = json.loads(raw)
        reply = j["choices"][0]["message"].get("content")
        if not isinstance(reply, str) or not reply.strip():
            log.warning("kimi: пустой или неожиданный ответ: %s", raw[:300])
            return None
        return reply.strip()
    except Exception as e:                      # сеть, таймаут, кривой JSON — всё мимо бота
        log.warning("kimi: запрос не прошёл: %r", e)
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
