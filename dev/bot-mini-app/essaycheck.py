"""«ИИ-проверка»: пайплайн разбора сочинения по фото (пуш 5 из PUSH-PLAN.md).

Этапы: сборка фото → распознавание рукописи через
yavision (модель handwritten; текст сохраняется ВМЕСТЕ С ОШИБКАМИ ученика,
ничего не исправляется) и чтение задания тем же OCR → поиск ошибок и разбор
по критериям ФИПИ (лёгкая модель kimi, строгий JSON, мышление стримится).
Координаты ошибок привязываются к словам OCR ДЕТЕРМИНИРОВАННО кодом (не
моделью). Критерии ФИПИ подставляются из кэша fipicache (ошибка кэша никогда
не ломает проверку — фолбэк CRITERIA ниже).

События генератора run(): ("status", "текст этапа") | ("think", «дельта
мышления Кими» — стрим «Вишенка думает…» на прелоадере) → … → ("done", result)
или ("err", "net|http|empty"). Сбой этапа — событие, а не падение: бот переводит
его в {"t":"fail"} для клиента (см. bot.api_essay_check_run). result нормализуется
в _result(): типы ошибок из закрытого списка, нумерация подряд, итог баллов
считается по критериям кодом — модели не доверяется её собственный «итог».

"""
import asyncio, json, logging, math, os, re

import fipicache, kimi, yavision


log = logging.getLogger("tutor.essaycheck")

TYPES = ("Орфография", "Пунктуация", "Речь")


WHAT = {"ege": "сочинения ЕГЭ", "oge": "эссе для ОГЭ"}
# Уточнение контекста фото (ТЗ: «с уточнением в промте»): сам промт OCR не принимает —
# Yandex Vision распознаёт фото без подсказок, поэтому контекст «сочинение ЕГЭ / эссе
# ОГЭ» уходит в промт анализа Кими и в логи этапа (см. run()).
CONTEXT = {"ege": "фото рукописного текста на русском: сочинение ЕГЭ",
           "oge": "фото рукописного текста на русском: эссе для ОГЭ"}
# Пустой src у ошибки не пропускается — подставляется этот шаблон (ТЗ: обоснование
# с источником обязательно у каждой ошибки).
SRC_TEMPLATE = "Справочник по русскому языку (источник не указан)"
CRITERIA = {
    "ege": "К1 Формулировка проблемы (0–1), К2 Комментарий (0–6), К3 Позиция автора (0–1), "
           "К4 Отношение к позиции (0–1), Речевые нормы (0–2), Орфография (0–3), Пунктуация (0–3) — итого 17",
    "oge": "К1 Раскрытие темы, К2 Аргументация, К3 Речевое оформление, К4 Грамотность "
           "(орфография, пунктуация, речевые нормы) — с актуальными максимумами ФИПИ для эссе ОГЭ",
}

TASK_BLOCK = "Условие задания и опорный текст (распознаны с фото задания):\n{task}"
# Задания нет (пользователь явно выбрал «Продолжить без») — жёсткая запретная формулировка.
TASK_MISSING = ("Условия и опорного текста нет — НЕ ВЫДУМЫВАЙ их и не используй "
                "несуществующий контекст.")

ANALYSIS_PROMPT = """Ты — опытный проверяющий {what} по русскому языку (критерии ФИПИ).
Контекст работы: {context}.
{task_block}
Ниже распознанный текст работы ученика — ошибки сохранены как есть, ничего не исправляй.
1) Найди ошибки: орфографические, пунктуационные и речевые. Для каждой ОБЯЗАТЕЛЬНО укажи:
   found — фрагмент работы ДОСЛОВНО, как написано учеником;
   correct — исправленный вариант;
   why — краткое правило, по которому это ошибка;
   src — источник теории: «Розенталь, §…» / «Лопатин, §…» / «Критерии ФИПИ, К…».
   Поле src НЕ оставляй пустым.
2) Разбери по критериям ФИПИ: {criteria}. По каждому критерию — карточка потерь баллов:
   name, got (набрано), max (максимум), why (обоснование), tip (совет, если балл не максимальный).
   Шкала ошибок: 0 ошибок — максимум, 1–2 — минус один балл, 3–4 — половина,
   5 и больше — 0 (орфография и пунктуация); речевые: 0 ошибок — максимум,
   1–2 — половина, 3 и больше — 0.
3) Составь до трёх рекомендаций: title (короткий заголовок), text (что сделать),
   badge (ожидаемый эффект, например «+1 К2»).
4) Координаты ошибок (page/x/y) бери из переданной ниже разметки слов OCR:
   page — индекс фото работы (0, 1, 2…), x/y — центр слова в процентах от размера фото.
   Если фрагмент не найден в разметке — не придумывай координаты.
Не выдумывай фактов, которых нет в работе/задании.
Верни СТРОГО JSON:
{{"errors":[{{"type":"Орфография|Пунктуация|Речь","found":"…","correct":"…","why":"…","src":"…","page":0,"x":0.0,"y":0.0}}],
  "criteria":[{{"name":"…","got":n,"max":n,"why":"…","tip":"…"}}],
  "recommendations":[{{"title":"…","text":"…","badge":"…"}}]}}"""


def _num(v, default, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):   # OverflowError: int(float("inf"))
        return default
    return max(lo, min(hi, n))


def _s(v):
    return v.strip() if isinstance(v, str) else ""


def _as_list(v):
    """Ответ модели — недоверенные данные: вместо списка может прийти число или объект.
    Итерируем только настоящие списки, иначе for по числу даёт TypeError."""
    return v if isinstance(v, list) else []


def _finite(v):
    """Число из ответа модели: не bool и конечно (inf/nan — не число, иначе OverflowError)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _result(a, essay, task):
    """Ответ модели → нормализованный разбор. Типы ошибок — из закрытого списка,
    номера идут подряд, баллы зажимаются в рамки критерия, итог — сумма критериев."""
    errors = []
    for e in _as_list(a.get("errors")):
        if not isinstance(e, dict) or not _s(e.get("found")):
            continue
        e2 = {"i": len(errors) + 1,
              "type": e.get("type") if e.get("type") in TYPES else "Речь",
              "found": _s(e.get("found")), "correct": _s(e.get("correct")),
              "why": _s(e.get("why")), "src": _s(e.get("src"))}
        # демо-оверлей: координаты ошибки на фото (page — индекс фото, x/y — проценты)
        for k, lo, hi in (("page", 0, 20), ("x", 0, 100), ("y", 0, 100)):
            v = _finite(e.get(k))
            if v is not None:
                e2[k] = max(lo, min(hi, int(v) if k == "page" else round(v, 1)))
        if all(k in e2 for k in ("x", "y")):
            e2.setdefault("page", 0)
        errors.append(e2)
    criteria = []
    for c in _as_list(a.get("criteria")):
        if not isinstance(c, dict) or not _s(c.get("name")):
            continue
        mx = _num(c.get("max"), 1, 1, 10)
        criteria.append({"name": _s(c.get("name")), "got": _num(c.get("got"), 0, 0, mx), "max": mx,
                         "why": _s(c.get("why")), "tip": _s(c.get("tip"))})
    recs = []
    for r in _as_list(a.get("recommendations")):
        if not isinstance(r, dict) or not _s(r.get("title")):
            continue
        recs.append({"title": _s(r.get("title")), "text": _s(r.get("text")), "badge": _s(r.get("badge"))})
        if len(recs) == 3:
            break
    got = sum(c["got"] for c in criteria)
    mx = sum(c["max"] for c in criteria)
    pct = round(got * 100 / mx) if mx else 0
    level = ("Отлично" if pct >= 85 else "Хорошо" if pct >= 70
             else "Удовлетворительно" if pct >= 50 else "Нужно доработать")
    return {"text": essay, "task": task, "errors": errors, "criteria": criteria,
            "recommendations": recs,
            "score": {"got": got, "max": mx, "percent": pct, "level": level}}


def _norm(s):
    """Нормализация для поиска фрагмента в словах OCR: ё→е, регистр, только буквы/цифры.
    Модель может прислать что угодно («found»: 2024 — число, или объект): не-строка
    даёт пустой фрагмент («не найден»), а не AttributeError."""
    return re.sub(r"[^0-9a-zа-я]+", "", (s if isinstance(s, str) else "").lower().replace("ё", "е"))


def _close(a, b):
    """Нечёткое совпадение токенов: равны или расхождение ≤1 символа (вставка/замена/удаление)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1 or not a or not b:
        return False
    if len(a) > len(b):
        a, b = b, a                      # a — короче
    # b длиннее ровно на 1: либо вставка в b, либо замена равной длины (тут не бывает)
    for i in range(len(b)):
        if a == b[:i] + b[i + 1:]:       # удаление одного символа из b
            return True
    return False


def _near(a, b):
    """Дешёвый префильтр перед _lev1 (звук: ни одно совпадение не отбрасывается).
    При расхождении ≤1 символа первые символы равны либо правка в позиции 0 —
    тогда равны «хвосты» со сдвигом: a[1:]==b[1:] (замена), a[1:]==b (вставка в a),
    a==b[1:] (вставка в b)."""
    if not a or not b:
        return False
    return a[0] == b[0] or a[1:2] == b[1:2] or a[1:2] == b[:1] or a[:1] == b[1:2]


def _lev1(a, b):
    """Расстояние Левенштейна ≤1 (равны / одна вставка / одна замена / одно удаление)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    if la > lb:
        a, b = b, a                                  # a — короче, b — длиннее ровно на 1 символ
    for i in range(len(b)):                          # одно удаление из b (эквивалентно вставке в b)
        if a == b[:i] + b[i + 1:]:
            return True
    return False


def _bind_coords(a, pages):
    """Детерминированная привязка ошибок к словам OCR: found → боксы слов (page/x/y в %).
    Поиск посимвольный по нормализованному тексту страницы (ё→е, без пунктуации), поэтому
    «считаю, что», «во-первых» и «Во первых» находятся даже при разном разбиении на слова;
    нечёткое совпадение — при расхождении ≤1 символа. Координаты проставляются ТОЛЬКО из
    разметки OCR (модель их не даёт); фрагмент не найден — без оверлея.
    pages — [{"text","words":[{t,x1,y1,x2,y2}]}] по страницам работы (индекс = page)."""
    prepared = []
    for pi, pg in enumerate(pages):
        ws, text, idx = [], "", []
        for w in _as_list(pg.get("words")):
            t = _norm(w.get("t"))
            if not t:
                continue
            ws.append(w)
            text += t
            idx += [len(ws) - 1] * len(t)
        prepared.append((pi, text, idx, ws))
    for e in _as_list(a.get("errors")):
        if not isinstance(e, dict):
            continue
        needle = _norm(e.get("found"))
        best = None                                  # (page, первое слово, последнее слово)
        for pi, text, idx, ws in prepared:
            if not needle or not text:
                continue
            n = len(needle)
            pos, ln = -1, 0
            i = text.find(needle)                    # 1) точное совпадение
            if i >= 0:
                pos, ln = i, n
            else:                                    # 2) нечёткое: ≤1 символ расхождения
                for L in (n - 1, n, n + 1):
                    if L <= 0:
                        continue
                    for j in range(0, len(text) - L + 1):
                        seg = text[j:j + L]
                        if _near(needle, seg) and _lev1(needle, seg):
                            pos, ln = j, L
                            break
                    if pos >= 0:
                        break
            if pos >= 0:
                best = (pi, ws[idx[pos]], ws[idx[pos + ln - 1]])
                break
        if not best:
            e.pop("page", None); e.pop("x", None); e.pop("y", None)   # координат нет — оверлея нет
            continue
        pi, w1, w2 = best
        x1 = min(float(w1["x1"]), float(w2["x1"])); x2 = max(float(w1["x2"]), float(w2["x2"]))
        y1 = min(float(w1["y1"]), float(w2["y1"])); y2 = max(float(w1["y2"]), float(w2["y2"]))
        e["page"], e["x"], e["y"] = pi, round((x1 + x2) / 2, 1), round((y1 + y2) / 2, 1)


def _parse_json(text):
    """Ответ модели → dict: вырезаем JSON из текста (терпимо к ```json и постороннему тексту)."""
    t = (text or "").strip()
    if not t:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
    try:
        j = json.loads(t)
        return j if isinstance(j, dict) else None
    except ValueError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(t[i:j + 1])
        return d if isinstance(d, dict) else None
    except ValueError:
        return None


def _msg(kind, task, essay, criteria_text):
    """Сборка промта анализа: контекст работы (ЕГЭ/ОГЭ), задание или жёсткий запрет его выдумывать."""
    task_block = TASK_BLOCK.format(task=task) if task else TASK_MISSING
    return (ANALYSIS_PROMPT.format(what=WHAT[kind], context=CONTEXT[kind],
                                   criteria=criteria_text, task_block=task_block)
            + "\n\nТекст работы (транскрипция OCR, ошибки сохранены):\n" + essay)


async def run(w, t, kind="ege"):
    """Пайплайн проверки: w — страницы работы, t — фото задания (base64 jpeg).
    Асинхронный генератор событий: status | think (стрим мышления Кими) → done(result) | err(why).

    Боевой путь: Yandex Vision OCR (handwritten) транскрибирует фото работы и задания,
    лёгкая Kimi анализирует текст и разметку слов; мышление Кими стримится событиями
    ("think", …) — фронт показывает его от лица Вишенки. Стриминг мышления Яндекс невозможен:
    API Vision его не отдаёт (по ТЗ допустимо стримить только Кими)."""
    kind = kind if kind in WHAT else "ege"
    yield "status", "Собрала %d фото в одно сочинение" % (len(w) + len(t))
    try:
        pages = await yavision.recognize_many(w)
    except yavision.VisionError as e:
        log.warning("essaycheck: OCR работы не прошёл (%s)", e.code)
        yield "err", e.code
        return
    essay = "\n\n".join(_s(p.get("text")) for p in pages).strip()
    if not essay:
        log.warning("essaycheck: OCR работы дал пустой текст")
        yield "err", "empty"
        return
    yield "status", "Распознан рукописный текст"
    task = ""
    if t:                                      # фото задания — условие и опорный текст
        try:
            tpages = await yavision.recognize_many(t)
        except yavision.VisionError as e:
            log.warning("essaycheck: OCR задания не прошёл (%s)", e.code)
            yield "err", e.code
            return
        task = "\n\n".join(_s(p.get("text")) for p in tpages).strip()
        yield "status", "Прочитано задание и опорный текст"
    yield "status", "Проверены орфографию и пунктуацию"
    extra = await fipicache.criteria(kind, "")   # актуальные документы ФИПИ (кэш 30 дней),
    criteria_text = CRITERIA[kind] + (("\n\nДополнительно — актуальные материалы ФИПИ с fipi.ru:\n" + extra)
                                      if extra else "")   # базовые критерии всегда в промте
    msg = _msg(kind, task, essay, criteria_text)
    parts, err = [], None
    async for ev, v in kimi.ask_stream([{"role": "user", "content": msg}],
                                       json_mode=True, thinking=True):
        if ev == "think":                      # стрим мышления Кими — на прелоадер от лица Вишенки
            yield "think", v
        elif ev == "text":
            parts.append(v)
        else:
            err = v
            break
    if err:
        log.warning("essaycheck: анализ не прошёл (%s)", err)
        yield "err", err if err in ("net", "http", "empty") else "net"
        return
    a = _parse_json("".join(parts))
    if not a:                                  # один повтор: модель вернула не-JSON
        log.warning("essaycheck: разбор не разобрался как JSON — повтор")
        a = await kimi.ask_json([{"role": "user", "content": msg}])
    if not isinstance(a, dict):
        log.warning("essaycheck: модель не ответила на разбор")
        yield "err", "net"
        return
    if not (a.get("errors") or a.get("criteria")):
        log.warning("essaycheck: разбор пустой")
        yield "err", "empty"
        return
    for e in _as_list(a.get("errors")):        # пустой src не пропускаем — подставляем шаблон
        if isinstance(e, dict) and not _s(e.get("src")):
            e["src"] = SRC_TEMPLATE
    # координаты ошибок — только из разметки OCR; поиск O(страницы × ошибки × фрагмент)
    # выносится из событийного цикла: иначе на 60 ошибках он морозит бота и API на секунды,
    # HEALTHCHECK (timeout=5s) не проходит и Render перезапускает сервис
    await asyncio.to_thread(_bind_coords, a, pages)
    yield "status", "Сверяю с критериями ФИПИ…"
    yield "done", _result(a, essay, task)
