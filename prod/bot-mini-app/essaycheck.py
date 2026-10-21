"""«ИИ-проверка»: пайплайн разбора сочинения по фото (пуш 5 из PUSH-PLAN.md).

Этапы: сборка фото → распознавание рукописи и чтение задания (vision; текст
сохраняется ВМЕСТЕ С ОШИБКАМИ ученика, ничего не исправляется) → поиск ошибок
и разбор по критериям ФИПИ (строгий JSON). Модель — kimi, как в «Чате с ИИ».

События генератора run(): ("status", "текст этапа") → … → ("done", result) или
("err", "net|http|empty"). Сбой этапа — событие, а не падение: бот переводит его
в {"t":"fail"} для клиента (см. bot.api_essay_check_run). result нормализуется
в _result(): типы ошибок из закрытого списка, нумерация подряд, итог баллов
считается по критериям — модели не доверяется её собственный «итог».
"""
import logging

import kimi

log = logging.getLogger("tutor.essaycheck")

TYPES = ("Орфография", "Пунктуация", "Речь")

WHAT = {"ege": "сочинения ЕГЭ", "oge": "эссе для ОГЭ"}
CRITERIA = {
    "ege": "К1 Формулировка проблемы (0–1), К2 Комментарий (0–6), К3 Позиция автора (0–1), "
           "К4 Отношение к позиции (0–1), Речевые нормы (0–2), Орфография (0–3), Пунктуация (0–3) — итого 17",
    "oge": "К1 Раскрытие темы, К2 Аргументация, К3 Речевое оформление, К4 Грамотность "
           "(орфография, пунктуация, речевые нормы) — с актуальными максимумами ФИПИ для эссе ОГЭ",
}

OCR_PROMPT = """Ты распознаёшь рукописные работы учеников.
Передано {n} фото: первые {w} — страницы сочинения по порядку{tail}.
Перепиши текст сочинения В ТОЧНОСТИ как написано рукой: сохраняй все ошибки,
пропуски и пунктуацию — ничего не исправляй и не стилизуй. Страницы соедини
в один текст.
Верни СТРОГО JSON: {{"essay":"…","task":"…"}} — task: условие задания и опорный
текст с фото задания (если фото задания нет — пустая строка)."""

ANALYSIS_PROMPT = """Ты — опытный проверяющий {what} по русскому языку (критерии ФИПИ).
Ниже условие задания и распознанный текст работы ученика (ошибки сохранены как есть).
1) Найди ошибки: орфографические, пунктуационные и речевые. Для каждой укажи фрагмент
   текста (found — дословно из работы), исправление (correct), краткое правило (why)
   и источник (src — например «Розенталь, §…» или «Правила орфографии и пунктуации, §…»).
2) Разбери по критериям ФИПИ: {criteria}. Для каждого критерия — набранные баллы (got),
   максимум (max), обоснование (why) и совет (tip), если балл не максимальный.
   Шкала ошибок: 0 ошибок — максимум, 1–2 — минус один балл, 3–4 — половина,
   5 и больше — 0 (орфография и пунктуация); речевые: 0 ошибок — максимум,
   1–2 — половина, 3 и больше — 0.
3) Составь до трёх рекомендаций: title (короткий заголовок), text (что сделать),
   badge (ожидаемый эффект, например «+1 К2»).
Верни СТРОГО JSON:
{{"errors":[{{"type":"Орфография|Пунктуация|Речь","found":"…","correct":"…","why":"…","src":"…"}}],
  "criteria":[{{"name":"…","got":n,"max":n,"why":"…","tip":"…"}}],
  "recommendations":[{{"title":"…","text":"…","badge":"…"}}]}}"""


def _num(v, default, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _s(v):
    return v.strip() if isinstance(v, str) else ""


def _result(a, essay, task):
    """Ответ модели → нормализованный разбор. Типы ошибок — из закрытого списка,
    номера идут подряд, баллы зажимаются в рамки критерия, итог — сумма критериев."""
    errors = []
    for e in a.get("errors") or []:
        if not isinstance(e, dict) or not _s(e.get("found")):
            continue
        errors.append({"i": len(errors) + 1,
                       "type": e.get("type") if e.get("type") in TYPES else "Речь",
                       "found": _s(e.get("found")), "correct": _s(e.get("correct")),
                       "why": _s(e.get("why")), "src": _s(e.get("src"))})
    criteria = []
    for c in a.get("criteria") or []:
        if not isinstance(c, dict) or not _s(c.get("name")):
            continue
        mx = _num(c.get("max"), 1, 1, 10)
        criteria.append({"name": _s(c.get("name")), "got": _num(c.get("got"), 0, 0, mx), "max": mx,
                         "why": _s(c.get("why")), "tip": _s(c.get("tip"))})
    recs = []
    for r in a.get("recommendations") or []:
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


async def run(w, t, kind="ege"):
    """Пайплайн проверки: w — страницы работы, t — фото задания (base64 jpeg).
    Асинхронный генератор событий: status → … → done(result) | err(why)."""
    kind = kind if kind in WHAT else "ege"
    yield "status", "Собрала %d фото в одно сочинение" % (len(w) + len(t))
    tail = ", остальные %d — условие задания и опорный текст" % len(t) if t else " (фото задания нет)"
    rec = await kimi.ask_json(
        [{"role": "user", "content": OCR_PROMPT.format(n=len(w) + len(t), w=len(w), tail=tail)}],
        images=w + t, max_tokens=8000)
    if not isinstance(rec, dict):
        log.warning("essaycheck: модель не ответила на распознавание")
        yield "err", "net"
        return
    if not _s(rec.get("essay")):
        log.warning("essaycheck: распознавание дало пустой текст")
        yield "err", "empty"
        return
    essay, task = _s(rec["essay"]), _s(rec.get("task"))
    yield "status", "Распознан рукописный текст"
    if t:
        yield "status", "Прочитано задание и опорный текст"
    yield "status", "Проверены орфография и пунктуация"
    msg = (ANALYSIS_PROMPT.format(what=WHAT[kind], criteria=CRITERIA[kind])
           + "\n\nУсловие задания:\n" + (task or "(фото задания не было)")
           + "\n\nТекст работы:\n" + essay)
    a = await kimi.ask_json([{"role": "user", "content": msg}])
    if not isinstance(a, dict):
        log.warning("essaycheck: модель не ответила на разбор")
        yield "err", "net"
        return
    if not (a.get("errors") or a.get("criteria")):
        log.warning("essaycheck: разбор пустой")
        yield "err", "empty"
        return
    yield "status", "Сверяю с критериями ФИПИ…"
    yield "done", _result(a, essay, task)
