"""Критерии ФИПИ для «ИИ-проверки»: кэш текста со сроком 30 дней + обновление с fipi.ru.

Обновление — официальной REST-ручкой Kimi `POST {KIMI_BASE_URL}/v1/tools/fetch`
(старый встроенный $web_search Kimi выведен из эксплуатации 20.10.2026). Ответ
очищается от мусора и сохраняется в файл кэша (переживает рестарты). Если fetch
не удался — работаем со СТАРОЙ версией кэша, при её отсутствии — со вшитым
текстом критериев (фолбэк). Ошибка кэша НИКОГДА не ломает проверку.

Ключ — только из env (KIMI_API_KEY, тот же, что у kimi.py).
"""
import asyncio
import json
import logging
import os
import re
import time

import aiohttp

import kimi

log = logging.getLogger("tutor.fipicache")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TTL = 30 * 24 * 3600          # критерии перечитываем раз в месяц
FETCH_TIMEOUT = 30.0
MAX_CRITERIA_TEXT = 4000     # потолок текста критериев в промте: компактная таблица, а не выжимка сайта


def _like(text):
    """Похоже ли выкачанное на таблицу критериев? Страницы fipi.ru — лендинги со ссылками на PDF:
    без этой проверки в промт уходил информационный мусор (лишние токены и путаница у модели)."""
    t = (text or "").lower()
    return bool(re.search(r"(?i)(к|ск)\s*1\b", t)) and "балл" in t

# страницы критериев на fipi.ru (демоверсии/кодификаторы — разделы с критериями оценивания)
URLS = {
    "ege": "https://fipi.ru/ege/demoversii-specifikacii-kodifikatory",
    "oge": "https://fipi.ru/oge/demoversii-specifikacii-kodifikatory",
}

_locks = {}
_cache_mem = {}               # kind -> {"ts":…, "text":…} — последняя удачная версия в памяти


def _path(kind):
    return os.path.join(DATA_DIR, "fipi_%s.json" % kind)


def _load(kind):
    try:
        with open(_path(kind), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("text"), str) and d["text"].strip():
            return {"ts": float(d.get("ts") or 0), "text": d["text"].strip()}
    except Exception:
        pass
    return _cache_mem.get(kind)


def _save(kind, text):
    _cache_mem[kind] = {"ts": time.time(), "text": text}
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_path(kind), "w", encoding="utf-8") as f:
            json.dump(_cache_mem[kind], f, ensure_ascii=False)
    except Exception as e:                       # диск недоступен — живём памятью, проверка не падает
        log.warning("fipicache: не сохранил кэш %s: %r", kind, e)


def _clean(raw):
    """HTML/текст страницы → компактный текст критериев: без тегов, скриптов и пустот."""
    if not isinstance(raw, str):
        return ""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = re.sub(r"&nbsp;?", " ", t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()[:20000]                     # потолок — чтобы не раздуть промт


async def _fetch(url):
    """Один вызов tools/fetch Kimi → текст страницы. Сбой → None (не исключение)."""
    body = {"url": url}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT)) as s:
            async with s.post(kimi.BASE + "/tools/fetch", json=body,
                              headers={"Authorization": "Bearer " + kimi.API_KEY}) as r:
                if r.status != 200:
                    log.warning("fipicache: tools/fetch HTTP %s", r.status)
                    return None
                j = await r.json(content_type=None)
    except Exception as e:
        log.warning("fipicache: tools/fetch не прошёл: %r", e)
        return None
    if isinstance(j, dict):
        for key in ("content", "text", "markdown", "result", "data"):
            v = j.get(key)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, dict):
                for k2 in ("content", "text", "markdown"):
                    if isinstance(v.get(k2), str) and v[k2].strip():
                        return v[k2]
    return None


async def criteria(kind, fallback=""):
    """Текст критериев для промта: свежий кэш → обновление с fipi.ru → старый кэш → fallback."""
    kind = kind if kind in URLS else "ege"
    cur = _load(kind)
    if cur and _like(cur["text"]) and time.time() - cur["ts"] < TTL:
        return cur["text"]
    if not kimi.enabled():                       # без ключа обновить нечем — что есть, то и берём
        t = (cur or {}).get("text") or ""
        return t if _like(t) else fallback
    lock = _locks.setdefault(kind, asyncio.Lock())
    async with lock:                             # параллельные проверки не дёргают fetch вдвойне
        cur = _load(kind)
        if cur and _like(cur["text"]) and time.time() - cur["ts"] < TTL:
            return cur["text"]
        raw = await _fetch(URLS[kind])
        text = _clean(raw)
        if len(text) > 200 and _like(text):       # мусор/пустая/не-критерии — не затираем кэш
            text = text[:MAX_CRITERIA_TEXT]
            _save(kind, text)
            log.info("fipicache: критерии %s обновлены (%d символов)", kind, len(text))
            return text
        log.warning("fipicache: fetch %s не дал критериев — работаю со старой версией", kind)
        t = (cur or {}).get("text") or ""
        return t if _like(t) else fallback
