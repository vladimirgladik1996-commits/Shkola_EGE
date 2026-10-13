"""STT-клиент (распознавание голоса, OpenAI-совместимый /audio/transcriptions — Whisper).

Подключение — переменными окружения (см. .env.example):
  WHISPER_API_KEY    — ключ сервиса распознавания (без него голос не распознаётся)
  WHISPER_BASE_URL   — по умолчанию https://api.openai.com/v1;
                       для Groq: https://api.groq.com/openai/v1 (модель whisper-large-v3-turbo)
  WHISPER_MODEL      — по умолчанию whisper-1
  WHISPER_LANG       — язык записи («ru»), по умолчанию ru
  WHISPER_TIMEOUT    — таймаут запроса в секундах, по умолчанию 60

Принимает ogg/opus (голосовые Telegram), webm/opus и mp4 (запись в мини-аппе), mp3, wav, m4a.
Любая ошибка — это None и запись в лог, а не исключение наверх: бот отвечает вежливой заглушкой.
"""
import json, logging, os

import aiohttp

log = logging.getLogger("tutor.whisper")

BASE = (os.getenv("WHISPER_BASE_URL", "").strip() or "https://api.openai.com/v1").rstrip("/")  # пустое значение = дефолт
API_KEY = os.getenv("WHISPER_API_KEY", "").strip()
MODEL = (os.getenv("WHISPER_MODEL", "") or "whisper-1").strip()
LANG = (os.getenv("WHISPER_LANG", "") or "ru").strip()
try: TIMEOUT = max(5, min(300, float(os.getenv("WHISPER_TIMEOUT", "60"))))
except ValueError: TIMEOUT = 60.0

# расширение файла для multipart: сервисы различают форматы именно по нему
_EXT = {"audio/ogg": "ogg", "audio/opus": "ogg", "audio/webm": "webm", "audio/mp4": "m4a",
        "audio/m4a": "m4a", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav",
        "audio/x-wav": "wav", "audio/flac": "flac", "audio/mpga": "mpga"}


def enabled():
    """Есть ли ключ STT. Без него голосовые сообщения не распознаются и наружу ничего не уходит."""
    return bool(API_KEY)


def _fname(mime):
    return "voice." + _EXT.get((mime or "").split(";")[0].strip().lower(), "webm")


async def transcribe(audio, mime="audio/webm"):
    """Текст записи (str) или None — нет ключа / сервис недоступен / не разобрал ответ."""
    if not API_KEY or not audio:
        return None
    form = aiohttp.FormData()
    form.add_field("model", MODEL)
    if LANG:
        form.add_field("language", LANG)
    form.add_field("file", audio, filename=_fname(mime), content_type=mime or "audio/webm")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as s:
            async with s.post(BASE + "/audio/transcriptions", data=form,
                              headers={"Authorization": "Bearer" + " " + API_KEY}) as r:
                raw = await r.text()
                if r.status != 200:
                    log.warning("whisper: HTTP %s: %s", r.status, raw[:300])
                    return None
        j = json.loads(raw)
        text = j.get("text")
        if not isinstance(text, str) or not text.strip():
            log.warning("whisper: пустой или неожиданный ответ: %s", raw[:300])
            return None
        return text.strip()
    except Exception as e:                      # сеть, таймаут, кривой JSON — всё мимо бота
        log.warning("whisper: запрос не прошёл: %r", e)
        return None
