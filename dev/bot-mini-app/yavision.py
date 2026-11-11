"""Yandex Vision OCR — модель handwritten: распознавание рукописного текста.

Используется «ИИ-проверкой» (essaycheck) для транскрипции фото работы и фото
задания, а также для разметки слов с координатами (чтобы Кими могла привязать
ошибки к местам на фото).

API: POST https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText
  заголовок: Authorization: Api-Key $YANDEX_API_KEY
  тело: {"mimeType":"JPEG","languageCodes":["ru"],"model":"handwritten","content":"<base64>"}

Важно: сам OCR промт не принимает — контекст «сочинение ЕГЭ / эссе ОГЭ»
передаётся дальше в промт Кими (см. essaycheck.ANALYSIS_PROMPT).

События/ошибки: сбой одного фото — повтор (1 раз), затем исключение EssayCheckError
с кодом "net" | "http" | "empty" — essaycheck превращает его в событие err.
Ключ ТОЛЬКО в env (YANDEX_API_KEY), никогда в код/логи/репозиторий.
"""
import asyncio
import base64
import json
import logging
import os
import random
import time

import aiohttp

log = logging.getLogger("tutor.yavision")

API = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"
API_KEY = os.environ.get("YANDEX_" "API_KEY", "").strip()
TIMEOUT = float(os.getenv("YANDEX_TIMEOUT", "60") or "60")
def _int(name, default, lo, hi):
    try:
        v = int(os.getenv(name, "") or default)
    except ValueError:
        v = default
    return max(lo, min(hi, v))


# Параллельность — главное ускорение OCR (12 фото по очереди = минуты тишины в стриме).
# Квота API бережётся повторами на 429; жёсткий потолок 12, настройка — env.
MAX_PARALLEL = _int("YANDEX_MAX_PARALLEL", 12, 1, 12)
RETRIES = _int("YANDEX_RETRIES", 2, 0, 5)      # повторов на одно фото (сеть/429/5xx)


class VisionError(Exception):
    """Сбой OCR. .code — "net" (сеть/таймаут/повторы) или "http" (API отказал)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code if code in ("net", "http") else "net"


def enabled():
    """Боевой режим OCR возможен, только если задан ключ."""
    return bool(API_KEY)


def _dim(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _box(bbox, w, h):
    """boundingBox.vertices (4 угла) → {x1,y1,x2,y2} в процентах (0–100, 1 знак).
    Координаты ответа — пиксели от размера фото (width/height в textAnnotation);
    если размер неизвестен — считаем значения уже нормализованными (0–1)."""
    verts = ((bbox or {}).get("vertices")) or []
    xs, ys = [], []
    for v in verts:
        if not isinstance(v, dict):
            continue
        try:
            xs.append(float(v.get("x", 0)))
            ys.append(float(v.get("y", 0)))
        except (TypeError, ValueError):
            continue
    if not xs or not ys:
        return None
    if w > 0:
        xs = [x * 100.0 / w for x in xs]
        ys = [y * 100.0 / h if h > 0 else y for y in ys]
    else:
        xs = [x * 100.0 if x <= 1.0 else x for x in xs]
        ys = [y * 100.0 if y <= 1.0 else y for y in ys]
    x1, x2 = min(xs), max(xs)
    y1, y2 = min(ys), max(ys)
    if x2 < 0 or y2 < 0 or x1 > 100 or y1 > 100:
        return None
    f = lambda n: round(max(0.0, min(100.0, n)), 1)
    return {"x1": f(x1), "y1": f(y1), "x2": f(x2), "y2": f(y2)}


def _walk_words(node, out, w, h):
    """Рекурсивно собирает слова с координатами: blocks → lines → words."""
    if not isinstance(node, dict):
        return
    text = node.get("text")
    if isinstance(text, str) and text.strip():
        box = _box(node.get("boundingBox"), w, h)
        if box:
            out.append({"t": text.strip(), **box})
    for key in ("blocks", "lines", "words", "entities", "tables"):
        child = node.get(key)
        if isinstance(child, list):
            for c in child:
                _walk_words(c, out, w, h)


def parse(j):
    """Ответ recognizeText → {"text":…, "words":[{t,x1,y1,x2,y2}]} (координаты в %).
    Разбор толерантный: схема ответа может пополняться полями."""
    # ответ терпимый к форме: гейтвей отдаёт {"result":{"textAnnotation":…}},
    # встречается и прямой {"textAnnotation":…} — оба варианта разбираются одинаково
    res = (j or {}).get("result")
    res = res if isinstance(res, dict) else (j or {})
    ta = (res.get("textAnnotation") if isinstance(res.get("textAnnotation"), dict) else {}) or {}
    w, h = _dim(ta.get("width")), _dim(ta.get("height"))
    words = []
    _walk_words(ta, words, w, h)
    text = ta.get("fullText") if isinstance(ta.get("fullText"), str) else ""
    return {"text": text.strip(), "words": words}


def parse_lw(j):
    """Только слова внутри строк (blocks → lines → words): [{t,x1,y1,x2,y2,li,s,n}].
    li — номер строки, s/n — позиция слова в fullText (textSegments), если API её отдал.
    Нужна перепроверке спорных слов (ocrverify): parse() держит строки и слова вперемешку.
    Терпима к любому мусору — при сбое разбора просто пустой список."""
    try:
        res = (j or {}).get("result")
        res = res if isinstance(res, dict) else (j or {})
        ta = res.get("textAnnotation") if isinstance(res.get("textAnnotation"), dict) else {}
        w, h = _dim(ta.get("width")), _dim(ta.get("height"))
        out, li = [], 0
        for b in ta.get("blocks") or []:
            for ln in (b.get("lines") if isinstance(b, dict) else None) or []:
                for wd in (ln.get("words") if isinstance(ln, dict) else None) or []:
                    t = wd.get("text") if isinstance(wd, dict) else None
                    box = _box(wd.get("boundingBox"), w, h) if isinstance(t, str) and t.strip() else None
                    if not box:
                        continue
                    e = {"t": t.strip(), **box, "li": li}
                    segs = wd.get("textSegments")
                    if isinstance(segs, list) and segs and isinstance(segs[0], dict):
                        try:
                            e["s"], e["n"] = int(segs[0].get("startIndex")), int(segs[0].get("length"))
                        except (TypeError, ValueError):
                            pass
                    out.append(e)
                li += 1
        return out
    except Exception:
        return []


async def _recognize_one(session, sem, img_b64, model="handwritten", retries=None):
    """Одно фото → {"text","words"[,"lw"]}. Повторы при сбое сети. model: handwritten | page."""
    body = {"mimeType": "JPEG", "languageCodes": ["ru"], "model": model,
            "content": img_b64}
    last = "net"
    t0 = time.monotonic()
    retries = RETRIES if retries is None else retries
    for attempt in range(retries + 1):
        try:
            async with sem:
                async with session.post(API, json=body,
                                        headers={"Authorization": "Api" + "-Key " + API_KEY},
                                        timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
                    if r.status != 200:
                        raw = await r.text()
                        log.warning("yavision: HTTP %s: %s", r.status, raw[:200])
                        last = "http"
                        if r.status in (408, 429, 500, 502, 503, 504) and attempt < retries:
                            await asyncio.sleep(min(8.0, 1.5 * 2 ** attempt) + random.uniform(0, 0.5))
                            continue
                        raise VisionError("http")
                    j = await r.json(content_type=None)
                    res = parse(j)
                    lw = parse_lw(j)
                    if lw:
                        res["lw"] = lw
                    if not res["text"] and not res["words"]:
                        log.warning("yavision: распознано пусто (фото без текста?)")
                    log.info("yavision: фото распознано dt=%.1fs попыток=%d слов=%d", time.monotonic() - t0,
                             attempt + 1, len(res.get("words") or []))
                    return res
        except VisionError:
            raise
        except Exception as e:                      # сеть, таймаут, кривой JSON
            log.warning("yavision: попытка %s не удалась: %r", attempt + 1, e)
            last = "net"
            if attempt < retries:
                await asyncio.sleep(min(8.0, 1.5 * 2 ** attempt) + random.uniform(0, 0.5))
    log.warning("yavision: фото не распознано dt=%.1fs попыток=%d причина=%s", time.monotonic() - t0,
                retries + 1, last)
    raise VisionError(last)


async def recognize_many(images, model="handwritten", parallel=None, retries=None):
    """Список фото (base64 jpeg) → список {"text","words"} по порядку страниц.
    Параллельность ограничена MAX_PARALLEL (env YANDEX_MAX_PARALLEL); parallel/retries — свои пределы на вызов
    (второй, необязательный прогон перепроверки берёт мало параллельных и без повторов, чтобы не отнимать
    квоту у основного).

    Надёжность: сбой ЧАСТИ фото не роняет проверку — вместо страницы подставляется
    заглушка {"text":"","words":[],"failed":True} (позиция в списке сохраняется —
    page у ошибок не съезжает), essaycheck помечает итог why="part". VisionError —
    только когда не удалось НИ ОДНО фото (или ключ не задан)."""
    if not images:
        return []
    if not enabled():
        raise VisionError("net")
    par = min(MAX_PARALLEL, parallel) if parallel else MAX_PARALLEL
    sem = asyncio.Semaphore(par)
    t0 = time.monotonic()

    async def safe(session, img):
        try:
            if retries is None:
                return await _recognize_one(session, sem, img, model), ""
            return await _recognize_one(session, sem, img, model, retries), ""
        except VisionError as e:
            return None, e.code
        except Exception as e:                      # страховка: один сбой не должен ронять остальных
            log.warning("yavision: непредвиденный сбой фото: %r", e)
            return None, "net"

    async with aiohttp.ClientSession() as session:
        out = await asyncio.gather(*[safe(session, img) for img in images])
    bad = sum(1 for res, _ in out if res is None)
    log.info("yavision: фото=%d распознано=%d сбой=%d dt=%.1fs параллельно<=%d", len(images), len(out) - bad, bad,
             time.monotonic() - t0, par)
    if all(res is None for res, _ in out):
        raise VisionError(next((code for _, code in out if code), "net"))
    return [res if res is not None else {"text": "", "words": [], "failed": True}
            for res, _ in out]
