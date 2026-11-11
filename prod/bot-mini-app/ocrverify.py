"""Перепроверка рукописи «ИИ-проверки»: два прогона Yandex OCR → спорные слова → Kimi по вырезкам.

Зачем. Yandex Vision OCR отдаёт ОДНО прочтение слова и не говорит, насколько он уверен (ни вариантов,
ни confidence в ответе нет). Поэтому «неуверенное» слово определяем сами — по расхождению двух моделей
Яндекса на одном и том же фото (handwritten и page): слова, прочитанные одинаково, считаем надёжными
(и настоящие ошибки ученика остаются как есть), расходящиеся — спорные.

Как.  compare(pages, pages2) — по координатам сопоставляет слова двух прогонов, возвращает спорные.
      resolve(images, pages, doubts) — вырезает каждое спорное слово с фото, одним-тремя запросами
      показывает вырезки Кими (vision) и правит текст страницы ТОЛЬКО при уверенном ответе.

Защита от «исправления по смыслу» (главный риск: языковая модель правит настоящие ошибки ученика):
  • Кими просят читать БУКВА В БУКВУ и не исправлять орфографию, выбор — между прочтениями A/B/«иначе»;
  • замена только при sure=true; выбор B — только если собственное прочтение Кими (read) совпало с B;
    «иначе» принимается лишь если прочитанное ≤ MAX_EDIT правок от A или B;
  • любой сбой (нет ключа, таймаут, не-JSON) — текст остаётся ровно таким, каким его дал Яндекс.

Выключатель: env EC_WORD_VERIFY=0. Лимиты: EC_VERIFY_MAX (слов на работу), EC_VERIFY_CHUNK
(вырезок в одном запросе), EC_VERIFY_TIMEOUT (сек на запросы к Кими), EC_VERIFY_OCR2_TIMEOUT (сек на второй прогон Яндекса от старта),
EC_VERIFY_GRACE (сек ожидания второго прогона после готовности основного).
"""
import asyncio
import base64
import io
import logging
import os
import re
import time

import kimi

log = logging.getLogger("tutor.ocrverify")


def _int(name, default, lo, hi):
    try:
        v = int(os.getenv(name, "") or default)
    except ValueError:
        v = default
    return max(lo, min(hi, v))


MAX_WORDS = _int("EC_VERIFY_MAX", 60, 0, 200)      # спорных слов на всю работу (приоритет — близкие прочтения)
CHUNK = _int("EC_VERIFY_CHUNK", 20, 5, 40)         # вырезок в одном запросе к Кими
PAR2 = _int("EC_VERIFY_PAR", 4, 1, 12)             # одновременных запросов второго прогона Яндекса (основной — до 12)
MAX_EDIT = 3                                       # «иначе»: не дальше стольких правок от A или B

_NORM_RE = re.compile(r"[^0-9a-zа-я]")
_TOK_RE = re.compile(r"^([^0-9A-Za-zА-Яа-яЁё]*)(.*?)([^0-9A-Za-zА-Яа-яЁё]*)$", re.S)


def timeout():
    try:
        return max(5.0, float(os.getenv("EC_VERIFY_TIMEOUT", "") or 15))
    except ValueError:
        return 15.0


def ocr2_timeout():
    """Сколько максимум ждём ВТОРОЙ прогон Яндекса (page): он необязателен, основная проверка его не ждёт дольше."""
    try:
        return max(5.0, float(os.getenv("EC_VERIFY_OCR2_TIMEOUT", "") or 20))
    except ValueError:
        return 20.0


def grace():
    """Сколько ещё ждём второй прогон ПОСЛЕ готовности основного OCR. Обычно он готов одновременно с основным;
    если он заметно медленнее — не задерживаем проверку: слова остаются как прочитал Яндекс (как до перепроверки)."""
    try:
        return max(0.5, float(os.getenv("EC_VERIFY_GRACE", "") or 4))
    except ValueError:
        return 4.0


def enabled():
    """Перепроверка идёт, только если не выключена и есть оба ключа (Yandex и Kimi)."""
    import yavision
    if os.getenv("EC_WORD_VERIFY", "1").strip() == "0" or MAX_WORDS <= 0:
        return False
    return bool(yavision.API_KEY) and bool(kimi.API_KEY)


def norm(s):
    """Для сравнения: регистр, ё=е, без пунктуации."""
    return _NORM_RE.sub("", (s or "").lower().replace("ё", "е"))


def _split(tok):
    m = _TOK_RE.match(tok or "")
    return (m.group(1), m.group(2), m.group(3)) if m else ("", tok or "", "")


def lev(a, b):
    """Расстояние Левенштейна."""
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _area(b):
    return max(0.0, b["x2"] - b["x1"]) * max(0.0, b["y2"] - b["y1"])


def _inter(a, b):
    w = min(a["x2"], b["x2"]) - max(a["x1"], b["x1"])
    h = min(a["y2"], b["y2"]) - max(a["y1"], b["y1"])
    return w * h if w > 0 and h > 0 else 0.0


def _page_doubts(pi, hw, pg):
    """Спорные слова одной страницы: слово handwritten ↔ слова page на том же месте фото."""
    hw = [(i, w) for i, w in enumerate(hw) if norm(w.get("t")) and _area(w) > 0]
    pg = [(j, w) for j, w in enumerate(pg) if norm(w.get("t")) and _area(w) > 0]
    cand, owners = {}, {}
    for i, a in hw:
        aa = _area(a)
        for j, b in pg:
            if b["y2"] < a["y1"] or b["y1"] > a["y2"]:          # быстрый отсев по вертикали
                continue
            if _inter(a, b) / min(aa, _area(b)) >= 0.5:
                cand.setdefault(i, []).append(j)
                owners.setdefault(j, []).append(i)
    pgd = dict(pg)
    out = []
    for i, a in hw:
        js = cand.get(i)
        if not js or any(len(owners[j]) != 1 for j in js):      # нет пары или слово page «накрывает» несколько
            continue                                            # слов handwritten — однозначного сравнения нет
        js.sort(key=lambda j: pgd[j]["x1"])
        b_text = " ".join(pgd[j]["t"] for j in js)
        na, nb = norm(a["t"]), norm("".join(pgd[j]["t"] for j in js))
        if na == nb or max(len(na), len(nb)) <= 1:
            continue
        out.append({"page": pi, "wi": i, "a": a["t"], "b": b_text, "d": lev(na, nb),
                    "box": {k: a[k] for k in ("x1", "y1", "x2", "y2")}})
    return out


def compare(pages, pages2):
    """pages — основной прогон (handwritten), pages2 — второй (page): список спорных слов
    (близкие прочтения — первыми), не больше MAX_WORDS. Страницы без «lw» или упавшие — пропускаются."""
    doubts = []
    for pi, (p1, p2) in enumerate(zip(pages or [], pages2 or [])):
        if not isinstance(p1, dict) or not isinstance(p2, dict) or p1.get("failed") or p2.get("failed"):
            continue
        hw, pg = p1.get("lw"), p2.get("lw")
        if isinstance(hw, list) and isinstance(pg, list) and hw and pg:
            doubts += _page_doubts(pi, hw, pg)
    doubts.sort(key=lambda d: d["d"])                           # сорт. устойчивая: порядок чтения сохраняется
    return doubts[:MAX_WORDS]


def _make_crops(images, doubts):
    """Вырезки спорных слов (base64 jpeg) в порядке doubts; None — не удалось. Блокирующая (PIL) —
    вызывать через asyncio.to_thread. Каждая страница декодируется один раз."""
    from PIL import Image
    out = [None] * len(doubts)
    for pi in sorted({d["page"] for d in doubts}):
        try:
            im = Image.open(io.BytesIO(base64.b64decode(images[pi]))).convert("RGB")
        except Exception as e:
            log.warning("ocrverify: страница %s не открылась для вырезок: %r", pi, e)
            continue
        W, H = im.size
        for k, d in enumerate(doubts):
            if d["page"] != pi:
                continue
            try:
                b = d["box"]
                x1, x2, y1, y2 = b["x1"] * W / 100, b["x2"] * W / 100, b["y1"] * H / 100, b["y2"] * H / 100
                px, py = max(6.0, (x2 - x1) * 0.15), max(6.0, (y2 - y1) * 0.35)
                c = im.crop((max(0, int(x1 - px)), max(0, int(y1 - py)), min(W, int(x2 + px)), min(H, int(y2 + py))))
                if c.width < 8 or c.height < 8:
                    continue
                if c.height < 96:                                # мелкие вырезки увеличиваем: так читается лучше
                    s = 96 / c.height
                    c = c.resize((max(1, int(c.width * s)), 96), Image.LANCZOS)
                if c.width > 1000:
                    c = c.resize((1000, max(1, int(c.height * 1000 / c.width))), Image.LANCZOS)
                buf = io.BytesIO()
                c.save(buf, "JPEG", quality=90)
                out[k] = base64.b64encode(buf.getvalue()).decode()
            except Exception as e:
                log.warning("ocrverify: вырезка слова не получилась: %r", e)
    return out


PROMPT = """Ты вычитываешь распознавание рукописного ученического сочинения на русском языке.
К сообщению приложено картинок: {n}. На каждой — ОДНО слово из рукописи (с небольшими полями), по порядку #1…#{n}.
Два распознавателя прочитали слово по-разному: вариант A и вариант B.

Для КАЖДОЙ картинки прочитай, что именно написано, БУКВА В БУКВУ.
Правила:
— НЕ исправляй орфографию и НЕ подбирай слово по смыслу: если ученик написал с ошибкой, читай так, как написано;
— read: ВСЕГДА впиши слово так, как оно написано на картинке (буква в букву), даже если оно неразборчиво — тогда напиши лучшее прочтение;
— pick: "A" — на картинке как в варианте A; "B" — как в варианте B; "X" — написано иначе; "?" — слово неразборчиво;
— порядок вариантов A и B ничего не значит: не угадывай, какой из них «правильный»;
— sure: true только если ты уверен в прочтении, при малейшем сомнении false.

Слова:
{items}

Ответ — строго JSON без пояснений: {{"r":[{{"i":1,"pick":"A","read":"","sure":true}}]}} — по одному объекту на каждое слово."""


def _case_like(src, new):
    """Регистр первой буквы (и ВСЕ ЗАГЛАВНЫЕ) — как у исходного слова."""
    if not new:
        return new
    if len(src) > 1 and src.isupper():
        return new.upper()
    if src[:1].isupper():
        return new[:1].upper() + new[1:]
    return new[:1].lower() + new[1:]


def _decide(d, item):
    """Ответ Кими по одному слову → новый ТОКЕН слова (с пунктуацией исходного) или None (не менять)."""
    if not isinstance(item, dict) or item.get("sure") is not True:
        return None
    pick = str(item.get("pick") or "").strip().upper()[:1]
    pre, a_core, post = _split(d["a"])
    na = norm(a_core)
    if pick == "B":
        # выбор B принимается, только если собственное прочтение Кими (read) совпадает с B: так «тяга к варианту B»
        # без реального чтения букв ничего не меняет
        if norm(item.get("read")) != norm(d["b"]):
            return None
        new_core = _split("".join(d["b"].split()))[1]
    elif pick == "X":
        new_core = _split(str(item.get("read") or "").strip())[1]
        nr = norm(new_core)
        nb = norm(d["b"])
        if len(nr) < 2 or min(lev(nr, na), lev(nr, nb)) > MAX_EDIT:
            return None
    else:
        return None
    if not norm(new_core) or norm(new_core) == na:
        return None
    return pre + _case_like(a_core, new_core) + post


async def _ask_chunk(jobs):
    """jobs — [(doubt, crop_b64)] → {индекс в jobs: ответ-словарь}. Любой сбой → пусто."""
    items = "\n".join('#%d: A="%s" B="%s"' % (k + 1, d["a"], d["b"]) for k, (d, _) in enumerate(jobs))
    msg = PROMPT.format(n=len(jobs), items=items)
    try:
        a = await kimi.ask_json([{"role": "user", "content": msg}], images=[c for _, c in jobs], max_tokens=2000)
    except Exception as e:
        log.warning("ocrverify: запрос к Кими не удался: %r", e)
        return {}
    r = (a or {}).get("r") if isinstance(a, dict) else None
    out = {}
    for it in r if isinstance(r, list) else []:
        try:
            i = int(it.get("i")) - 1
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= i < len(jobs):
            out[i] = it
    return out


def _patch_words(words, box, old, new):
    """«words» страницы (строки и слова вперемешку, нужны привязке ошибок к фото): тот же токен — на месте."""
    for e in words or []:
        try:
            t = e.get("t")
            if not isinstance(t, str):
                continue
            inside = (e["x1"] <= box["x1"] + 0.2 and e["y1"] <= box["y1"] + 0.2
                      and e["x2"] >= box["x2"] - 0.2 and e["y2"] >= box["y2"] - 0.2)
            if not inside:
                continue
            if t == old:
                e["t"] = new
            elif old in t:
                e["t"] = t.replace(old, new, 1)
        except (KeyError, TypeError):
            continue


def _apply(page, fixes):
    """fixes — [(индекс в page["lw"], старый токен, новый токен)] → список применённых {from,to}.
    Позиция слова в fullText — из textSegments (если совпало с текстом), иначе поиск по порядку чтения."""
    text, lw = page.get("text") or "", page.get("lw") or []
    pos, cur = {}, 0
    for k, w in enumerate(lw):
        t, s, n = w["t"], w.get("s"), w.get("n")
        p = s if isinstance(s, int) and isinstance(n, int) and text[s:s + n] == t else text.find(t, cur)
        if p is not None and p >= 0:
            pos[k] = p
            cur = p + len(t)
    done = []
    for wi, old, new in sorted(fixes, key=lambda f: -pos.get(f[0], -1)):    # с конца: ранние позиции не съезжают
        p = pos.get(wi)
        if p is None or text[p:p + len(old)] != old:
            continue
        text = text[:p] + new + text[p + len(old):]
        w = lw[wi]
        _patch_words(page.get("words"), {k: w[k] for k in ("x1", "y1", "x2", "y2")}, old, new)
        w["t"] = new
        done.append({"from": old, "to": new})
    page["text"] = text
    return done


async def resolve(images, pages, doubts):
    """Спорные слова → вырезки → Кими → правка pages НА МЕСТЕ (только при уверенном ответе).
    Возвращает статистику {"doubts","asked","fixed"}. Правка применяется в самом конце одним куском:
    таймаут/сбой посреди запросов оставляет pages нетронутыми."""
    t0 = time.monotonic()
    crops = await asyncio.to_thread(_make_crops, images, doubts)
    jobs = [(d, c) for d, c in zip(doubts, crops) if c]
    chunks = [jobs[i:i + CHUNK] for i in range(0, len(jobs), CHUNK)]
    answers = await asyncio.gather(*[_ask_chunk(ch) for ch in chunks])
    per_page = {}
    for ch, ans in zip(chunks, answers):
        for k, item in ans.items():
            d = ch[k][0]
            new = _decide(d, item)
            if new and new != d["a"]:
                per_page.setdefault(d["page"], []).append((d["wi"], d["a"], new))
    fixed = []
    for pi, fx in per_page.items():
        done = _apply(pages[pi], fx)
        if done:
            pages[pi]["vfix"] = (pages[pi].get("vfix") or []) + done
            fixed += done
    log.info("ocrverify: спорных=%d вырезок=%d запросов=%d исправлено=%d dt=%.1fs",
             len(doubts), len(jobs), len(chunks), len(fixed), time.monotonic() - t0)
    return {"doubts": len(doubts), "asked": len(jobs), "fixed": len(fixed)}
