"""STT-клиент (распознавание голоса, OpenAI-совместимый /audio/transcriptions — Whisper).

Подключение — переменными окружения (см. .env.example):
  WHISPER_API_KEY    — ключ сервиса распознавания (без него голос не распознаётся)
  WHISPER_BASE_URL   — по умолчанию https://api.openai.com/v1;
                       для Groq: https://api.groq.com/openai/v1 (модель whisper-large-v3-turbo)
  WHISPER_MODEL      — по умолчанию whisper-1
  WHISPER_LANG       — язык записи («ru»), по умолчанию ru
  WHISPER_TIMEOUT    — таймаут запроса в секундах, по умолчанию 60

Тишина и «выдумки»: на пустой записи Whisper склонен галлюцинировать (дописывает «Продолжение следует…»,
«Спасибо за просмотр» и т.п.). Поэтому запрос идёт с temperature=0 и verbose_json, а ответ проверяется:
вероятность «речи нет» (no_speech_prob), повторяющийся текст (compression_ratio) и физически невозможная скорость
речи (> MAX_CPS символов в секунду по длительности записи). Итог transcribe(): текст, "" — речи в записи нет,
None — сбой сервиса. Так тишина получает честный ответ, а выдумка не уходит дальше как «команда».

Принимает ogg/opus (голосовые Telegram), webm/opus и mp4 (запись в мини-аппе), mp3, wav, m4a.
Любая ошибка — это None и запись в лог, а не исключение наверх: бот отвечает вежливой заглушкой.
"""
import json, logging, os, re

import aiohttp

log = logging.getLogger("tutor.whisper")

BASE = (os.getenv("WHISPER_BASE_URL", "").strip() or "https://api.openai.com/v1").rstrip("/")  # пустое значение = дефолт
API_KEY = os.getenv("WHISPER_API_KEY", "").strip()
MODEL = (os.getenv("WHISPER_MODEL", "") or "whisper-1").strip()
LANG = (os.getenv("WHISPER_LANG", "") or "ru").strip()
try: TIMEOUT = max(5, min(300, float(os.getenv("WHISPER_TIMEOUT", "60"))))
except ValueError: TIMEOUT = 60.0

NO_SPEECH_MSG = "Не услышал речи в записи — похоже, вы молчали."
NO_SPEECH_P = 0.6        # no_speech_prob выше — сегмент считается тишиной (вместе с низким avg_logprob)
NO_SPEECH_HARD = 0.9     # выше этого — тишина независимо от avg_logprob
LOGPROB_MIN = -1.0
MAX_COMPRESSION = 2.4    # compression_ratio выше — зацикленный повтор (типичная галлюцинация)
MAX_CPS = 25.0           # человек не говорит быстрее ~25 символов/с: больше — это выдумка, а не речь
ANTI = "Дословная расшифровка речи. Если речи нет — ничего не придумывай. "

# расширение файла для multipart: сервисы различают форматы именно по нему
_EXT = {"audio/ogg": "ogg", "audio/opus": "ogg", "audio/webm": "webm", "audio/mp4": "m4a",
        "audio/m4a": "m4a", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav",
        "audio/x-wav": "wav", "audio/flac": "flac", "audio/mpga": "mpga"}


def enabled():
    """Есть ли ключ STT. Без него голосовые сообщения не распознаются и наружу ничего не уходит."""
    return bool(API_KEY)


def _fname(mime):
    return "voice." + _EXT.get((mime or "").split(";")[0].strip().lower(), "webm")


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def clean(j, dur=None):
    """Ответ verbose_json → текст записи или "" (речи нет: тишина либо выдумка Whisper).
    dur — длительность записи в секундах (если известна клиенту; иначе берётся из ответа сервиса)."""
    if not isinstance(j, dict):
        return ""
    text = (j.get("text") or "").strip() if isinstance(j.get("text"), str) else ""
    segs = [x for x in (j.get("segments") or []) if isinstance(x, dict)] if isinstance(j.get("segments"), list) else []
    if segs:
        def bad(x):
            nsp, lp, cr = _num(x.get("no_speech_prob")), _num(x.get("avg_logprob")), _num(x.get("compression_ratio"))
            return ((nsp is not None and nsp > NO_SPEECH_P and (lp is None or lp < LOGPROB_MIN))
                    or (nsp is not None and nsp > NO_SPEECH_HARD) or (cr is not None and cr > MAX_COMPRESSION))
        good = [x for x in segs if not bad(x)]
        if not good:
            return ""                                  # все сегменты — тишина или зацикленный повтор
        if len(good) < len(segs):
            text = " ".join(str(x.get("text") or "").strip() for x in good).strip()
    if not text:
        return ""
    d = _num(dur) or _num(j.get("duration"))
    if d and d > 0 and len(text) / max(d, 1.0) > MAX_CPS:
        return ""                                      # физически невозможная скорость речи — это выдумка
    return text


async def transcribe(audio, mime="audio/webm", prompt="", dur=None):
    """Текст записи (str); "" — речи в записи нет (тишина / выдумка Whisper); None — нет ключа, сервис
    недоступен или ответ не разобран. prompt — подсказка (имена, термины): поднимает качество распознавания;
    dur — длительность записи в секундах, если знает клиент (для отсечения «невозможной» речи)."""
    if not API_KEY or not audio:
        return None
    form = aiohttp.FormData()
    form.add_field("model", MODEL)
    form.add_field("temperature", "0")                 # без «творчества»: детерминированная расшифровка
    form.add_field("response_format", "verbose_json")  # сегменты с no_speech_prob — по ним отличаем тишину от речи
    if LANG:
        form.add_field("language", LANG)
    names = re.sub(r"\s+", " ", str(prompt or "")).strip()
    form.add_field("prompt", (ANTI + names[:max(0, 500 - len(ANTI))])[:500])
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
        if not isinstance(j, dict) or not isinstance(j.get("text"), str):
            log.warning("whisper: неожиданный ответ: %s", raw[:300])
            return None
        text = clean(j, dur)
        if not text:
            log.info("whisper: речи в записи нет (тишина или выдумка отсечена)")
        return text
    except Exception as e:                      # сеть, таймаут, кривой JSON — всё мимо бота
        log.warning("whisper: запрос не прошёл: %r", e)
        return None
