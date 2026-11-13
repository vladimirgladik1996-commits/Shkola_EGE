"""«ИИ-проверка»: пайплайн разбора сочинения по фото (пуш 5 из PUSH-PLAN.md).

Этапы: сборка фото → распознавание рукописи через
yavision (модель handwritten; текст сохраняется ВМЕСТЕ С ОШИБКАМИ ученика,
ничего не исправляется) и чтение задания тем же OCR → поиск ошибок и разбор
по критериям ФИПИ (лёгкая модель kimi, строгий JSON, мышление стримится).
Координаты ошибок привязываются к словам OCR ДЕТЕРМИНИРОВАННО кодом (не
моделью). Критерии ФИПИ подставляются из кэша fipicache (ошибка кэша никогда
не ломает проверку — фолбэк CRITERIA ниже).

События генератора run(): ("status", "текст этапа") | ("think", «дельта
мышления Кими» — стрим «Вишенка думает…» на прелоадере) → … → ("done", result).
ГЛАВНОЕ ПРАВИЛО: пайплайн ВСЕГДА доходит до финального превью и НИЧЕГО не выдумывает:
сбой любого этапа — это тоже "done" с result, где поля missing перечисляют блоки без
данных (фронт показывает их пустыми), а why — честная причина сбоя; result нормализуется
в _result(): типы ошибок из закрытого списка, нумерация подряд, итог баллов считается
по критериям кодом — модели не доверяется её собственный «итог».

"""
import asyncio, json, logging, math, os, re, time

import fipicache, kimi, ocrverify, yavision


log = logging.getLogger("tutor.essaycheck")

TYPES = ("Орфография", "Пунктуация", "Грамматика", "Речь")


WHAT = {"ege": "сочинения ЕГЭ", "oge": "эссе для ОГЭ"}
# Уточнение контекста фото (ТЗ: «с уточнением в промте»): сам промт OCR не принимает —
# Yandex Vision распознаёт фото без подсказок, поэтому контекст «сочинение ЕГЭ / эссе
# ОГЭ» уходит в промт анализа Кими и в логи этапа (см. run()).
CONTEXT = {"ege": "фото рукописного текста на русском: сочинение ЕГЭ",
           "oge": "фото рукописного текста на русском: эссе для ОГЭ"}
# Пустой src у ошибки не пропускается — подставляется этот шаблон (ТЗ: обоснование
# с источником обязательно у каждой ошибки).
SRC_TEMPLATE = "Справочник по русскому языку (источник не указан)"
# Эталонные критерии ФИПИ 2027 (демоверсии ФИПИ + мр. ФИПИ/СТАТГРАД). Формат — компактная таблица
# с максимумами и шкалами: это одновременно источник истины для нормализации баллов кодом (CANON)
# и текст для промта. Компактность — сознательная экономия токенов: в промт идёт только то,
# по чему модель реально ставит баллы, а не многостраничные таблицы и не выжимки со страниц fipi.ru.
CRITERIA = {
    "ege": (
        "ЕГЭ-2027 (проект ФИПИ), 22 балла. Карточки только К1–К6; К7–К10 (грамотность) НЕ оценивай — "
        "их считает код по списку ошибок.\n"
        "К1 Отражение позиции автора (0–1): 1 — позиция по проблеме сформулирована верно; 0 — нет или неверна. "
        "Если К1=0, К2 и К3 = 0.\n"
        "К2 Комментарий к позиции (0–3): 3 — опора на текст: 2 примера-иллюстрации, пояснение к каждому, "
        "смысловая связь между ними указана и верно пояснена; 2 — 2 примера с пояснениями, связь не указана, "
        "неверна или не пояснена; 1 — 1 пример с пояснением; 0 — пример без пояснения, примеров нет, комментарий "
        "без опоры на текст, пересказ или длинная цитата. Пример без пояснения не засчитывается. Фактическая "
        "ошибка — в К4, не в К2.\n"
        "К3 Собственное отношение (0–2): 2 — отношение обосновано + пример-аргумент (читательский, "
        "историко-культурный, жизненный опыт); 1 — обосновано без примера-аргумента, или пример есть, но "
        "отношение не заявлено; 0 — формальное «согласен/не согласен», обоснования нет или оно не по проблеме "
        "текста. Пример-аргумент не повторяет суждения автора; комиксы, аниме, манга, фанфики, графические "
        "романы, компьютерные игры — не источник.\n"
        "К4 Фактическая точность (0–1): 1 — фактических ошибок нет; 0 — есть.\n"
        "К5 Логичность (0–2): 2 — логических ошибок нет; 1 — одна-две; 0 — три и больше.\n"
        "К6 Этические нормы (0–1): 1 — нарушений нет; 0 — есть.\n"
        "Ловушки: «притянутая» связь между примерами, пояснение подменено пересказом, подмена тезиса, "
        "пример-аргумент пересказывает автора.\n"
        "Флаги (работа не оценивается): copied — переписан или пересказан исходный текст; "
        "off_text — работа не по данному тексту."
    ),
    "oge": (
        "ОГЭ-2027, задание 13 (проект ФИПИ), сочинение — 8 баллов. Карточки только СК1–СК4; грамотность "
        "(ГК1–ГК4, ФК1) оценивается вместе с изложением — не возвращай.\n"
        "СК1 Ответ на вопрос (0–1): 1 — прямой развёрнутый ответ на вопрос темы (тезис) с опорой на текст "
        "(минимум — указан автор); для 13.3 — с опорой на нравственные понятия; 0 — ответа нет, он неверный "
        "или не прямой.\n"
        "СК2 Примеры и пояснения (0–4): 4 — два примера из прочитанного текста, к каждому пояснение; "
        "3 — два примера, пояснение к одному; 2 — два без пояснений или один с пояснением; 1 — один без "
        "пояснения; 0 — примеров из текста нет. Жизненный опыт не засчитывается.\n"
        "СК3 Логичность (0–2): 2 — логических ошибок нет; 1 — одна-две; 0 — три и больше.\n"
        "СК4 Композиционная стройность (0–1): 1 — трёхчастная композиция (тезис, примеры, вывод), ошибок "
        "построения нет; 0 — композиция нарушена или есть ошибка построения.\n"
        "Ловушки: пример без пояснения; ответ на вопрос не прямой; вывод не вытекает из примеров.\n"
        "Флаги (работа не оценивается): copied — переписан или пересказан текст; off_question — сочинение "
        "на другой вопрос; off_text — работа не по данному тексту."
    ),
}

# Эталонная таблица (код, подпись карточки, максимум): баллы зажимаются по НЕЙ, а не по тому,
# что прислала модель, поэтому итог всегда в шкале ФИПИ — 22 (ЕГЭ) / 8 (ОГЭ).
CANON = {
    "ege": (("К1", "К1 Отражение позиции автора", 1), ("К2", "К2 Комментарий к позиции", 3),
            ("К3", "К3 Собственное отношение", 2), ("К4", "К4 Фактическая точность", 1),
            ("К5", "К5 Логичность", 2), ("К6", "К6 Этические нормы", 1),
            ("К7", "К7 Орфография", 3), ("К8", "К8 Пунктуация", 3),
            ("К9", "К9 Грамматика", 3), ("К10", "К10 Речевые нормы", 3)),
    "oge": (("СК1", "СК1 Ответ на вопрос", 1), ("СК2", "СК2 Примеры и пояснения", 4),
            ("СК3", "СК3 Логичность", 2), ("СК4", "СК4 Композиционная стройность", 1)),
}

# Минимальный объём работы (правило ФИПИ): меньше — работа не засчитывается, по всем критериям 0.
MIN_WORDS = {"ege": 150, "oge": 70}


def _envint(name, default):
    try:
        v = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return v if v > 0 else default


# Явные потолки генерации для двух запросов проверки. Они БОЛЬШИЕ (десятки тысяч токенов при ответе в
# несколько тысяч) и служат не ограничением, а предохранителем от зацикливания и от пробного 400
# «max_tokens» (лишний круг после каждого рестарта). Мышление входит в max_tokens, поэтому у запроса
# критериев потолок равен максимуму K2.6 (65 536), а у запроса ошибок (без мышления) — 32 768.
EC_MAX_ERR = _envint("KIMI_EC_MAX_ERR", 32768)
EC_MAX_CRIT = _envint("KIMI_EC_MAX_CRIT", 65536)

# Грамотность ЕГЭ (К7–К10) считает КОД по числу найденных ошибок своего типа: ФИПИ — 0 → 3, 1–2 → 2,
# 3–4 → 1, 5 и больше → 0. Модель по этим критериям не оценивает.
LANG_TYPES = {"К7": "Орфография", "К8": "Пунктуация", "К9": "Грамматика", "К10": "Речь"}


def _lang_score(n):
    return 3 if n == 0 else 2 if n <= 2 else 1 if n <= 4 else 0


# Критерии, которые оценивают работу относительно исходного текста: без фото задания их балл приблизительный,
# и карточка честно об этом говорит. Формулировка без слов «задание/условие/опорный» — её не режет _no_task.
NOTASK_CODES = {"ege": ("К1", "К2", "К3", "К4"), "oge": ("СК1", "СК2")}
NOTASK_NOTE = "Оценка приблизительная: исходного текста под рукой нет, точность ниже. "

# Флаги «работа не оценивается» (ставит модель критериев, применяет код): ключ → причина для карточек.
_FLAG_WHY = (("copied", "работа — переписанный или пересказанный исходный текст"),
             ("off_text", "работа написана не по данному тексту"),
             ("off_question", "сочинение написано на другой вопрос"))

TASK_BLOCK = "Условие задания и опорный текст (распознаны с фото задания):\n{task}"
# Задания нет (пользователь явно выбрал «Продолжить без» или не снимал его) — жёсткая
# запретная формулировка + ДЕТЕРМИНИРОВАННЫЙ фильтр _no_task() вырезает любые упоминания
# задания/условия/опорного текста из ответа модели. Промта одного недостаточно: модель
# всё равно «вспоминала» несуществующее условие (баг-репорт 006).
TASK_MISSING = ("ФОТО ЗАДАНИЯ НЕ БЫЛО: условия и опорного текста нет — не выдумывай их. Разбор — только по "
                "тексту работы; не упоминай задание, условие и опорный текст нигде в ответе и мышлении.")

ERRORS_PROMPT = """Ты — проверяющий {what} по русскому языку. Ниже OCR-текст рукописи ученика ({context}); ошибки сохранены, ничего не исправляй.
Найди ВСЕ ошибки по нормам современного литературного языка. type:
Орфография — написание слов; Пунктуация — лишние и пропущенные знаки (авторскую не засчитывай, сомнительное не включай); Грамматика — формы слов, словообразование, синтаксис (управление, согласование, строение предложения, видовременная соотнесённость); Речь — лексика и стиль (значение слова, сочетаемость, тавтология, лишние слова, штампы, смешение стилей).
Поля: type; found — 1–4 слова ДОСЛОВНО из текста; correct; why — правило кратко; src — «Розенталь, §…» / «Лопатин, §…» / «Критерии ФИПИ, К…» (не пусто).
Правила: 1) обрывок слова, набор букв, явный сбой распознавания рукописи — не ошибка ученика, пропусти; 2) не уверен — не включай, found совпадает с текстом буква в букву; 3) одно место — одна ошибка и один тип; 4) содержание и баллы не оценивай; без рассуждений, сразу JSON.
JSON: {{"errors":[{{"type":"Орфография|Пунктуация|Грамматика|Речь","found":"…","correct":"…","why":"…","src":"…"}}]}}"""

CRITERIA_PROMPT = """Ты — эксперт-проверяющий {what} по критериям ФИПИ ({context}).
Мысли и внутренние рассуждения (thinking) веди СТРОГО на русском языке — они показываются пользователю, мыслить по-английски нельзя.
{task_block}
Разбери работу по таблице: карточка на КАЖДЫЙ критерий — name (код и имя из таблицы), got, max, why (1–2 конкретных предложения: что в работе даёт балл или снимает его), tip (совет, если балл не максимальный). Баллы целые, в пределах max. Ошибки и грамотность не разбирай.
{criteria}
Рекомендации (до 3): title, text, badge (ожидаемый эффект, например «+1 К2»). flags — true только при явном нарушении, reason — одна фраза. Без фото задания flags off_text и off_question — false. Не выдумывай фактов, которых нет в работе или задании.
JSON: {{"flags":{{"copied":false,"off_text":false,"off_question":false,"reason":""}},"criteria":[{{"name":"…","got":n,"max":n,"why":"…","tip":"…"}}],"recommendations":[{{"title":"…","text":"…","badge":"…"}}]}}"""


_TASK_RE = re.compile(r"задани|услови|опорн", re.I)


def _no_task(s):
    """Ссылки на задание/условие/опорный текст, когда фото задания НЕ БЫЛО: каждое предложение
    с такой ссылкой вырезается целиком. Это чистка ВЫДУМОК, а не данных: пустая строка честнее,
    чем придуманное условие (ГЛАВНОЕ ПРАВИЛО — ничего не выдумываем). Работает детерминированно:
    одной прометки модели недостаточно (баг-репорт 006)."""
    t = _s(s)
    if not t:
        return ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", t) if p.strip()]
    return " ".join(p for p in parts if not _TASK_RE.search(p)).strip()


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


_WORDS_RE = re.compile(r"[a-zа-яё]+(?:-[a-zа-яё]+)*", re.I)
_INITIALS_RE = re.compile(r"(?:[А-ЯЁA-Z]\.\s*){1,2}(?=[А-ЯЁ][а-яё]+)")   # «М.Ю. Лермонтов» — одно слово
_ABBR_RE = re.compile(r"(?<![a-zа-яё])[а-яё]\.\s?[а-яё]\.(?:\s?[а-яё]\.)?", re.I)   # «т.д.», «т.е.» — не слова


def _words(t):
    """Слова работы по правилам ФИПИ (сноска к демоверсии ЕГЭ-2027): дефисные слова — одно слово
    («всё-таки»), инициалы с фамилией — одно слово, сокращения («т.д.») не считаются, цифры и знаки
    препинания не считаются («5 лет» — одно слово, «пять лет» — два)."""
    s = t if isinstance(t, str) else ""
    s = _INITIALS_RE.sub("", s)
    s = _ABBR_RE.sub(" ", s)
    return len(_WORDS_RE.findall(s))


_CODE_RE = re.compile(r"(?i)(ск|гк|фк|к)\s*(\d{1,2})(?!\d)")
# старые/свободные имена карточек → код эталонной таблицы (если модель не проставила код)
_ALIAS = (("орфограф", "К7"), ("пунктуац", "К8"), ("грамматич", "К9"), ("речев", "К10"),
          ("логич", "К5"), ("фактич", "К4"), ("этич", "К6"), ("комментар", "К2"),
          ("отношени", "К3"), ("позици", "К1"), ("проблем", "К1"),
          ("ответ на вопрос", "СК1"), ("пример", "СК2"), ("композиц", "СК4"))


def _crit_key(name, codes):
    """Код эталонного критерия из имени карточки: «К2 Комментарий» → К2, «Орфография» → К7.
    Незнакомое имя — «»: карточка не в счёт, баллы не выдумываем."""
    s = _s(name)
    m = _CODE_RE.search(s)
    code = (m.group(1) + m.group(2)).upper() if m else ""
    if code in codes:
        return code
    low = s.lower()
    for word, aliased in _ALIAS:
        if word in low and aliased in codes:
            return aliased
    return ""


def _infer_kind(a):
    """Тип работы из карточек (СК* — ОГЭ), когда он не передан явно (пересборка отчёта из истории)."""
    for c in _as_list(a.get("criteria")):
        if isinstance(c, dict) and re.search(r"(?i)ск\s*\d", _s(c.get("name"))):
            return "oge"
    return "ege"


def _canon(kind, raw, clean, errors=None):
    """Карточки модели → строго эталонная таблица ФИПИ: состав и максимумы — из CANON, баллы
    зажимаются кодом. Пустой список от модели — пустой результат (missing: criteria); критерии,
    которых модель не прислала, получают 0 с честной пометкой «нет данных», а не выдуманные баллы.
    Правило ФИПИ: К1 = 0 → К2 и К3 оцениваются в 0."""
    codes = {c[0] for c in CANON[kind]}
    by = {}
    for c in _as_list(raw):
        if isinstance(c, dict):
            k = _crit_key(c.get("name"), codes)
            if k and k not in by:
                by[k] = c
    if not by:
        return []
    has_k1 = "К1" in by                            # правило ниже — только если К1 именно оценён в 0,
    out = []                                       # а не когда модели его не прислала («нет данных» ≠ «0»)
    for code, label, mx in CANON[kind]:
        c = by.get(code)
        if errors is not None and kind == "ege" and code in LANG_TYPES:
            n = sum(1 for e in errors if e.get("type") == LANG_TYPES[code])   # грамотность — по ошибкам, кодом
            out.append({"name": label, "got": _lang_score(n), "max": mx,
                        "why": ("Найдено ошибок: %d (список — в блоке «Ошибки»)." % n) if n
                               else "Ошибок этого типа не найдено.",
                        "tip": ""})
            continue
        out.append({"name": label, "got": _num(c.get("got"), 0, 0, mx) if c else 0, "max": mx,
                    "why": clean(_s(c.get("why"))) if c else "нет данных от модели",
                    "tip": clean(_s(c.get("tip"))) if c else ""})
    if kind == "ege" and has_k1 and out[0]["got"] == 0:
        for i in (1, 2):
            if out[i]["got"]:
                out[i]["why"] = "К1 = 0: по правилам ФИПИ К2 и К3 оцениваются в 0. " + out[i]["why"]
            out[i]["got"] = 0
    return out


def _zero_reason(kind, flags, task):
    """Причина обнуления по флагам модели критериев или «». Флаги — недоверенные данные: только строгий
    True; «не по тексту/не на тот вопрос» не судим, когда задания не было (сравнивать не с чем)."""
    if not isinstance(flags, dict):
        return ""
    for key, why in _FLAG_WHY:
        if flags.get(key) is not True:
            continue
        if key in ("off_text", "off_question") and not _s(task):
            continue
        if key == "off_question" and kind != "oge":
            continue
        return why
    return ""


def _result(a, essay, task, why="", kind=""):
    """Ответ модели → нормализованный разбор. Типы ошибок — из закрытого списка,
    номера идут подряд, баллы зажимаются в рамки критерия, итог — сумма критериев.

    ГЛАВНОЕ ПРАВИЛО: разбор ВСЕГДА доходит до финального превью и НИЧЕГО не выдумывает:
    поля missing перечисляют блоки БЕЗ данных (text/errors/criteria/recommendations) —
    фронт показывает такие блоки пустыми, а не «додуманными» выводами; why — честная
    причина (код сбоя этапа), если данные получить не удалось."""
    kind = kind if kind in WHAT else _infer_kind(a)       # тип работы для эталонной таблицы ФИПИ
    clean = _no_task if not _s(task) else (lambda s: s)   # нет фото задания — выдумки о нём вырезаются
    errors = []
    for e in _as_list(a.get("errors")):
        if not isinstance(e, dict) or not _s(e.get("found")):
            continue
        miss = bool(e.pop("_miss", False)) or e.get("unconfirmed") is True
        if miss:
            # found не нашёлся в РАСПОЗНАННОМ тексте: при фрагментарном OCR слова могли просто не
            # распознаться — раньше такая ошибка выбрасывалась как «выдуманная» и разбор терял её
            # (нарушение обработки ошибок при недостатке слов в тексте, баг-репорт 018). Теперь
            # ошибка остаётся с честной пометкой «не подтверждена текстом» и без привязки к фото.
            log.warning("essaycheck: ошибка не подтверждена распознанным текстом (found=%r)", _s(e.get("found"))[:40])
        e2 = {"i": len(errors) + 1,
              "type": e.get("type") if e.get("type") in TYPES else "Речь",
              "found": _s(e.get("found")), "correct": _s(e.get("correct")),
              "why": clean(_s(e.get("why"))), "src": clean(_s(e.get("src"))) or SRC_TEMPLATE}
        if miss:
            e2["unconfirmed"] = True
        # демо-оверлей: координаты ошибки на фото (page — индекс фото, x/y — проценты)
        for k, lo, hi in (("page", 0, 20), ("x", 0, 100), ("y", 0, 100)):
            v = _finite(e.get(k))
            if v is not None:
                e2[k] = max(lo, min(hi, int(v) if k == "page" else round(v, 1)))
        if all(k in e2 for k in ("x", "y")):
            e2.setdefault("page", 0)
        errors.append(e2)
    # К7–К10 по числу ошибок — только для свежего разбора (run ставит _lang), и только если блок ошибок
    # реально получен: пересборка отчёта из истории берёт готовые карточки и баллов не меняет.
    lang = (a.get("_lang") is True and "errors" in a
            and not (not errors and _as_list(a.get("errors"))))
    criteria = _canon(kind, a.get("criteria"), clean, errors if lang else None)   # эталонная таблица, баллы — кодом
    n_words = _words(essay)
    if criteria and essay and n_words < MIN_WORDS[kind]:
        # Объём меньше минимума ФИПИ. Раньше код ОБНУЛЯЛ все критерии — но текст приходит из OCR,
        # и при фрагментарном распознавании (недостаток слов в тексте) нормальная работа получала
        # 0/8 и 0/22 (баг-репорт 018). Теперь баллы сохраняются, а правило ФИПИ о незачёте работы
        # донесено честной пометкой на каждой карточке. Идемпотентно — пометка не удваивается.
        note = ("Объём %d слов (минимум %d): на экзамене такую работу не засчитывают (0 баллов); "
                "если текст распознан не полностью — объём мог занизиться. " % (n_words, MIN_WORDS[kind]))
        for c in criteria:
            if note not in c["why"]:
                c["why"] = note + c["why"]
    zero = _zero_reason(kind, a.get("flags"), task)
    too_short = bool(essay) and n_words < MIN_WORDS[kind]
    if criteria and not _s(task) and not zero and not too_short:
        for c in criteria:                     # без задания баллы по содержанию — приблизительные
            if c["name"].split()[0] in NOTASK_CODES[kind] and NOTASK_NOTE not in c["why"]:
                c["why"] = NOTASK_NOTE + c["why"]   # (обнулённая работа пометки не получает: балл не «приблизительный»)
    if criteria and zero:
        # Правило ФИПИ: пересказ/переписанный текст, работа не по тексту или на другой вопрос — по всем критериям 0.
        znote = "По правилам ФИПИ работа не оценивается: %s. " % zero
        for c in criteria:
            c["got"] = 0
            if znote not in c["why"]:
                c["why"] = znote + c["why"]
    recs = []
    for r in _as_list(a.get("recommendations")):
        if not isinstance(r, dict) or not _s(r.get("title")):
            continue
        recs.append({"title": clean(_s(r.get("title"))), "text": clean(_s(r.get("text"))),
                     "badge": clean(_s(r.get("badge")))})
        if len(recs) == 3:
            break
    got = sum(c["got"] for c in criteria)
    mx = sum(c["max"] for c in criteria)
    pct = round(got * 100 / mx) if mx else 0
    level = ("Отлично" if pct >= 85 else "Хорошо" if pct >= 70
             else "Удовлетворительно" if pct >= 50 else "Нужно доработать")
    # блоки без данных: их нельзя оценивать и нельзя додумывать — фронт покажет их пустыми
    missing = []
    if not essay:
        missing.append("text")
    if "errors" not in a or (not errors and _as_list(a.get("errors"))):
        # списка нет или ни одна ошибка не подтвердилась текстом — это НЕ вердикт «0 ошибок»
        missing.append("errors")
    if not criteria:                  # реальный разбор всегда даёт критерии ФИПИ
        missing.append("criteria")
    if "recommendations" not in a:
        missing.append("recommendations")
    return {"text": essay, "task": task, "errors": errors, "criteria": criteria,
            "recommendations": recs, "missing": missing, "why": why,
            "score": {"got": got, "max": mx, "percent": pct, "level": level}}


# Дефис в конце строки — это ЛИБО перенос («пере-»/«несённых» → «перенесённых»), ЛИБО часть
# слова («что-»/«то», «по-»/«моему»). Раньше дефис терялся всегда: «кто-нибудь» превращалось
# в «ктонибудь», «по-моему» в «помоему» — в «печатной версии» появлялось слово, которого
# ученик не писал, и проверка находила несуществующую орфографическую ошибку.
_HY_PRON = frozenset("что кто где куда когда как какой какая какое какие какого каким каком какому "
                     "чей чья чьё чьи чего кого чему кому чем кем чём ком почему зачем откуда сколько "
                     "который которая которое которые".split())
_HY_ANY_RIGHT = frozenset({"либо", "нибудь", "таки"})              # кто-либо, где-нибудь, всё-таки
_HY_KOE = frozenset({"кое", "кой"})                                 # кое-как, кое-что
_HY_PO = frozenset({"по", "во", "в"})                               # по-русски, по-моему, во-первых
_HY_PO_RIGHT = re.compile(
    r"^(?:[а-яё]*(?:ски|ьи)|моему|твоему|нашему|вашему|своему|новому|старому|прежнему|другому|иному|"
    r"разному|первых|вторых|третьих|четвёртых|четвертых|пятых)(?![а-яё])", re.I)
_HY_LEFT_RE = re.compile(r"([а-яёa-z]+)-$", re.I)
_HY_RIGHT_RE = re.compile(r"[а-яёa-z]+", re.I)


def _keep_hyphen(cur, nxt):
    """True — дефис в конце cur принадлежит слову (оставляем), False — это перенос (склеиваем)."""
    ml, mr = _HY_LEFT_RE.search(cur), _HY_RIGHT_RE.match(nxt)
    if not ml or not mr:
        return False
    left, right = ml.group(1).lower(), mr.group(0).lower()
    if right in _HY_ANY_RIGHT or left in _HY_KOE:
        return True
    if right == "то" and left in _HY_PRON:
        return True
    if left == "из" and right in ("за", "под"):
        return True
    return left in _HY_PO and bool(_HY_PO_RIGHT.match(right))


def _reflow(text):
    """Реконструкция абзацев OCR-текста (ДЕТЕРМИНИРОВАННО, без LLM).
    OCR отдаёт строки по визуальным переносам рукописи — на экране это «рандомный набор
    предложений на разных строках». Функция убирает soft-wrap: склеивает переносы по дефису
    и хвосты строк внутри предложения, а новый абзац открывает только на красной строке или
    после конца предложения с заглавной. ОШИБКИ НЕ ИСПРАЛЯЕТ и слова не меняет — только
    структура (баг-репорт 018: структура эссе). Идемпотентна."""
    if not isinstance(text, str) or not text.strip():
        return text if isinstance(text, str) else ""
    raw = text.replace("\r\n", "\n").split("\n")
    lines = [l.rstrip() for l in raw]
    paras, cur = [], ""
    def flush():
        nonlocal cur
        if cur.strip():
            paras.append(cur.strip())
        cur = ""
    for ln in lines:
        s = ln.strip()
        if not s:
            flush()                      # пустая строка — разрыв абзаца
            continue
        # перенос по дефису: «сло-» + «во» -> «слово»
        if cur.endswith("-") and not cur.endswith("--"):
            cur = (cur if _keep_hyphen(cur, s) else cur[:-1]) + s
            continue
        if not cur:
            cur = s
            continue
        # новая строка-абзац: предыдущая кончилась .!?… и текущая с заглавной, либо явный отступ
        ends = cur.rstrip().endswith((".", "!", "?", "…"))
        upper = s[0].isupper() if s else False
        indented = ln[:1].isspace()
        if (ends and upper) or indented:
            flush()
            cur = s
        else:
            cur = cur.rstrip() + " " + s
    flush()
    return "\n\n".join(paras)


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


def _search(text, needle):
    """Поиск фрагмента в нормализованном тексте: точный, затем нечёткий (≤1 символа).
    Возвращает (позиция, длина) или (-1, 0)."""
    n = len(needle)
    if not needle or not text:
        return -1, 0
    i = text.find(needle)                        # 1) точное совпадение
    if i >= 0:
        return i, n
    for L in (n - 1, n, n + 1):                  # 2) нечёткое: ≤1 символ расхождения
        if L <= 0:
            continue
        for j in range(0, len(text) - L + 1):
            seg = text[j:j + L]
            if _near(needle, seg) and _lev1(needle, seg):
                return j, L
    return -1, 0


def _bind_coords(a, pages):
    """Детерминированная привязка ошибок к словам OCR: found → боксы слов (page/x/y в %).
    Поиск посимвольный по нормализованному тексту страницы (ё→е, без пунктуации), поэтому
    «считаю, что», «во-первых» и «Во первых» находятся даже при разном разбиении на слова;
    нечёткое совпадение — при расхождении ≤1 символа. Координаты проставляются ТОЛЬКО из
    разметки OCR (модель их не даёт); фрагмент не найден — без оверлея.

    Корпус склеен по ВСЕМ страницам сразу: фрагмент на стыке страниц («…человека.
    Я считаю…») не укладывается внутрь одной страницы — раньше он не находился, ошибка
    уходила в «без привязки» и печаталась под фото, а не отмечалась на нём. Теперь такой
    фрагмент получает координаты слова на той странице, где у него больше символов.
    Ищем дважды: сначала по СЛОВАМ (точный бокс слова), затем по всем записям — parse()
    держит строки и слова вперемешку, и попадание в строку дало бы бокс всей строки
    (маркер в её центре вместо места ошибки); запасной путь нужен, когда слова без
    боксов и в разметке остались только строки.
    Ошибка с found, которого НЕТ нигде в тексте работы, помечается _miss=True — это
    ВЫДУМКА модели, _result() её не показывает.
    pages — [{"text","words":[{t,x1,y1,x2,y2}]}] по страницам работы (индекс = page)."""
    corpora = []
    for only_words in (True, False):
        text, idx, spans = "", [], []
        for pi, pg in enumerate(pages):
            for w in _as_list(pg.get("words")):
                raw = _s(w.get("t")) if isinstance(w, dict) else ""
                t = _norm(raw)
                if not t or (only_words and re.search(r"\s", raw)):
                    continue                        # только слова: строки/блоки — запасной корпус
                text += t
                idx += [len(spans)] * len(t)
                spans.append((pi, w))
        corpora.append((text, idx, spans))
    alltext = "".join(_norm(pg.get("text")) for pg in pages)   # весь текст работы склейкой (стыки страниц — тоже текст)
    for e in _as_list(a.get("errors")):
        if not isinstance(e, dict):
            continue
        needle = _norm(e.get("found"))
        best = None                                  # (page, слова фрагмента на этой странице)
        for text, idx, spans in corpora:
            if not needle or not text:
                continue
            pos, ln = _search(text, needle)
            if pos < 0:
                continue
            cnt = {}
            for j in range(pos, pos + ln):
                pi = spans[idx[j]][0]
                cnt[pi] = cnt.get(pi, 0) + 1
            pi = max(cnt, key=lambda p: (cnt[p], -p))   # страница с большинством символов фрагмента
            seen, ents = set(), []
            for j in range(pos, pos + ln):
                sp = spans[idx[j]]
                if sp[0] == pi and id(sp[1]) not in seen:
                    seen.add(id(sp[1])); ents.append(sp[1])
            best = (pi, ents)
            break
        if not best:
            e.pop("page", None); e.pop("x", None); e.pop("y", None)   # координат нет — оверлея нет
            # нашлось хоть где-то в распознанном тексте (в т.ч. на границе страниц или в слове
            # без бокса) — просто нет координат; не нашлось нигде — фрагмент выдуман моделью,
            # помечаем для _result()
            e["_miss"] = _search(alltext, needle)[0] < 0   # ни в словах, ни в склейке текста — выдумка
            continue
        e.pop("_miss", None)
        pi, ents = best
        x1 = min(float(w["x1"]) for w in ents); x2 = max(float(w["x2"]) for w in ents)
        y1 = min(float(w["y1"]) for w in ents); y2 = max(float(w["y2"]) for w in ents)
        e["page"], e["x"], e["y"] = pi, round((x1 + x2) / 2, 1), round((y1 + y2) / 2, 1)


def _letters(s):
    """Буквы и цифры без регистра (ё=е) — чем сравниваем текст и слова OCR: пунктуация и пробелы не важны."""
    return "".join(ch for ch in (s or "").lower().replace("ё", "е") if ch.isalnum())


APPLY_MAX = 20000      # символов в поправленном тексте (сочинение + запас)


def apply_text(ocr, new_text):
    """Ученик поправил распознанный текст (слово или целый фрагмент) → (ocr с новым текстом, синхронизировано).
    Слова OCR (lw и words страниц — по ним ошибки привязываются к фото) подгоняются под правки, поэтому ошибка внутри
    исправленного места садится на те же боксы слов, что и неверно распознанный фрагмент: «синхронно с разметкой».
    Соответствие ищется по БУКВАМ: поток букв слов OCR = поток букв распознанного текста (переносы, пунктуация и
    абзацы на него не влияют); правки — словарная diff (difflib) исправленного текста против распознанного.
    Единица выравнивания — подслово (слово OCR может нести несколько слов текста после прежних правок): слово заменено →
    текст ложится на боксы покрытых слов (лишние пустеют, лишний текст дописывается подсловами после последнего
    покрытого); вставка — подсловами после предыдущего слова; удаление — подслова убираются. Исходный ocr не меняется.
    Не удалось выровнять (потоки букв разошлись) → текст принимается, слова не трогаются, синхронизировано=False."""
    import copy
    import difflib
    new_text = str(new_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:APPLY_MAX]
    out = dict(ocr)
    out["essay"] = new_text
    old = ocr.get("essay") or ""
    if new_text == old:
        return out, True
    pages = copy.deepcopy(ocr.get("pages") or [])
    out["pages"] = pages
    toks = [(pi, k) for pi, pg in enumerate(pages) if isinstance(pg, dict)
            for k, w in enumerate(pg.get("lw") or []) if isinstance(w, dict) and _letters(w.get("t"))]
    subs = [pages[pi]["lw"][k]["t"].split() for pi, k in toks]              # подслова каждого слова OCR
    units = [(x, i) for x, ss in enumerate(subs) for i, sub in enumerate(ss) if _letters(sub)]
    ow, nw = old.split(), new_text.split()
    onorm, nnorm = [_letters(x) for x in ow], [_letters(x) for x in nw]
    if "".join(_letters(subs[x][i]) for x, i in units) != "".join(onorm):
        return out, False

    def spans(lens):
        pos, r = 0, []
        for n in lens:
            r.append((pos, pos + n))
            pos += n
        return r
    usp, osp = spans([len(_letters(subs[x][i])) for x, i in units]), spans([len(x) for x in onorm])
    ops = [op for op in difflib.SequenceMatcher(None, onorm, nnorm, autojunk=False).get_opcodes() if op[0] != "equal"]
    before = [" ".join(ss) for ss in subs]
    for _tag, i1, i2, j1, j2 in reversed(ops):          # с конца: индексы подслов ранних правок не съезжают
        words = nw[j1:j2]
        a = osp[i1][0] if i1 < len(osp) else (osp[-1][1] if osp else 0)
        b = osp[i2 - 1][1] if i2 > i1 else a
        cov = [u for u, (ts, te) in enumerate(usp) if ts >= a and te <= b and te > ts]
        if not cov:                                    # вставка: подсловами после предыдущего слова
            if words and units:
                x, i = units[max([u for u, (ts, te) in enumerate(usp) if te <= a] or [0])]
                subs[x][i + 1:i + 1] = words
            continue
        for idx, u in enumerate(cov):
            x, i = units[u]
            subs[x][i] = words[idx] if idx < len(words) else None
        if len(words) > len(cov):                      # слов стало больше, чем покрыто: хвост — после последнего покрытого
            x, i = units[cov[-1]]
            subs[x][i + 1:i + 1] = words[len(cov):]
    from ocrverify import _patch_words
    for x, (pi, k) in enumerate(toks):
        nt = " ".join(t for t in subs[x] if t)
        if nt != before[x]:
            w = pages[pi]["lw"][k]
            _patch_words(pages[pi].get("words"), {c: w[c] for c in ("x1", "y1", "x2", "y2")}, w["t"], nt)
            w["t"] = nt
    return out, True


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
    """Промт запроса КРИТЕРИЕВ (К1–К6 / СК1–СК4 + рекомендации): контекст работы (ЕГЭ/ОГЭ), задание или
    жёсткий запрет его выдумывать, текст работы — в конце (общая часть промта стоит в начале)."""
    task_block = TASK_BLOCK.format(task=task) if task else TASK_MISSING
    return (CRITERIA_PROMPT.format(what=WHAT[kind], context=CONTEXT[kind],
                                   criteria=criteria_text, task_block=task_block)
            + "\n\nТекст работы (транскрипция OCR, ошибки сохранены):\n" + essay)


def _msg_errors(kind, essay):
    """Промт запроса ОШИБОК: задание и критерии не нужны — только текст работы (без мышления)."""
    return (ERRORS_PROMPT.format(what=WHAT[kind], context=CONTEXT[kind])
            + "\n\nТекст работы (транскрипция OCR, ошибки сохранены):\n" + essay)


async def _ocr_phase(w, t, kind):
    """Этап 1 проверки — РАСПОЗНАВАНИЕ: фото → текст работы и задания (Yandex OCR, перепроверка спорных слов).
    События: status…; затем либо done(result) — работа не распозналась (как раньше), либо ocr(данные для этапа 2):
    {pages, tpages, essay, task, extra, part, t_ocr, kind}. Анализ (ошибки, критерии ФИПИ) — этап 2, run(ocr=…)."""
    kind = kind if kind in WHAT else "ege"
    t0 = time.monotonic()                      # тайминги этапов — одной строкой в конце (essaycheck: проверка …)
    yield "status", "Собрала %d фото в одно сочинение" % (len(w) + len(t))
    # OCR работы, OCR задания и критерии ФИПИ — три независимых сетевых этапа: гоняются
    # ПАРАЛЛЕЛЬНО (скорость) и не роняют друг друга (надёжность).
    async def _ocr(images):
        if not images:
            return [], ""
        try:
            return await yavision.recognize_many(images), ""
        except yavision.VisionError as e:
            return None, e.code
        except Exception as e:                     # страховка: пайплайн не должен падать
            log.warning("essaycheck: OCR упал непредвиденно: %r", e)
            return None, "net"

    async def _criteria():
        try:
            return await fipicache.criteria(kind, "")   # актуальные документы ФИПИ (кэш 30 дней)
        except Exception as e:
            log.warning("essaycheck: кэш ФИПИ не дал критерии: %r", e)
            return ""

    async def _ocr2(images):
        """Второй прогон работы другой моделью Яндекса (page) — только чтобы найти спорные слова
        (Яндекс не отдаёт ни вариантов, ни уверенности). Строго best-effort: любой сбой → None."""
        if not images or not ocrverify.enabled():
            return None
        try:
            return await asyncio.wait_for(
                yavision.recognize_many(images, model="page", parallel=ocrverify.PAR2, retries=0), ocrverify.ocr2_timeout())
        except Exception as e:                 # в т.ч. TimeoutError: дольше ocr2_timeout() основную проверку не держим
            log.warning("essaycheck: второй прогон OCR не удался (%r) — перепроверка слов пропущена", e)
            return None

    main = asyncio.gather(_ocr(w), _ocr(t), _criteria())    # основной OCR стартует первым
    # второй прогон идёт ПАРАЛЛЕЛЬНО основному (скорость: время OCR не растёт), но с ограниченной параллельностью и
    # без повторов (качество/надёжность: основной OCR не теряет квоту Яндекса), а его сбой/таймаут ничего не ломает
    t2 = asyncio.ensure_future(_ocr2(w)) if w else None
    try:
        (pages, werr), (tpages, terr), extra = await main
        t_ocr = time.monotonic() - t0          # OCR работы, OCR задания и критерии идут параллельно
        pages2 = None
        if t2 is not None:
            if pages and not werr:
                try:                           # обычно второй прогон готов одновременно с основным; если он заметно
                    pages2 = await asyncio.wait_for(asyncio.shield(t2), ocrverify.grace())   # медленнее — не ждём
                except asyncio.TimeoutError:
                    log.warning("essaycheck: второй прогон OCR не успел за %.0f с после основного — перепроверка пропущена",
                                ocrverify.grace())
                    t2.cancel()
            else:
                t2.cancel()
    finally:
        if t2 is not None and not t2.done():
            t2.cancel()
    # перепроверка спорных слов: расхождение двух моделей Яндекса → вырезка слова → Кими читает буква в букву.
    # Текст меняется только при уверенном ответе; любой сбой оставляет распознанное Яндексом как есть.
    if pages and not werr and pages2 and len(pages2) == len(pages):
        try:
            doubts = ocrverify.compare(pages, pages2)
            if doubts:
                yield "status", "Перепроверяю неразборчивые слова"      # только когда спорные слова реально нашлись
                await asyncio.wait_for(ocrverify.resolve(w, pages, doubts), ocrverify.timeout())
        except Exception as e:                 # CancelledError/GeneratorExit сюда не попадают (BaseException)
            log.warning("essaycheck: перепроверка слов не удалась (%r) — остаётся распознанное Яндексом", e)
    task = _reflow("\n\n".join(_s(p.get("text")) for p in (tpages or [])).strip())
    # часть фото не распозналась — итог честно помечаем (фронт: «часть данных получить не удалось»)
    part = any(isinstance(p, dict) and p.get("failed") for p in list(pages or []) + list(tpages or []))
    if werr:
        log.warning("essaycheck: OCR работы не прошёл (%s)", werr)
        yield "done", _result({}, "", task, why=werr)   # превью с пустыми блоками, а не тупик
        return
    essay = _reflow("\n\n".join(_s(p.get("text")) for p in (pages or [])).strip())
    if not essay:
        log.warning("essaycheck: OCR работы дал пустой текст")
        yield "done", _result({}, "", task, why="empty")
        return
    log.info("essaycheck: OCR и критерии готовы за %.1fs kind=%s страниц работы=%d задания=%d слов=%d "
             "fipi.ru=%d симв.", t_ocr, kind, len(pages or []), len(tpages or []), _words(essay), len(extra or ""))
    yield "status", "Распознан рукописный текст"
    if t:                                      # фото задания — условие и опорный текст
        if terr:
            # задание не прочиталось — НЕ выдумываем его и НЕ бросаем проверку:
            # анализ идёт дальше с жёстким запретом выдумывать (TASK_MISSING)
            log.warning("essaycheck: OCR задания не прошёл (%s) — продолжаю без задания", terr)
        else:
            yield "status", "Прочитано задание и опорный текст"
    yield "ocr", {"pages": pages, "tpages": tpages, "essay": essay, "task": task, "extra": extra or "", "part": part,
                  "t_ocr": t_ocr, "kind": kind}


async def recognize(w, t, kind="ege"):
    """Этап 1 для «Текст проверен»: только распознавание. События: status | done(result — сбой) | ocr(данные)."""
    kind = kind if kind in WHAT else "ege"
    async for ev in _ocr_phase(w, t, kind):
        yield ev


async def run(w, t, kind="ege", ocr=None):
    """Пайплайн проверки: w — страницы работы, t — фото задания (base64 jpeg).
    Асинхронный генератор событий: status | think (стрим мышления Кими) → done(result) | err(why).
    ocr — данные этапа 1 (recognize), если распознавание уже прошло отдельно: тогда сразу анализ текста.

    Боевой путь: Yandex Vision OCR (handwritten) транскрибирует фото работы и задания,
    лёгкая Kimi анализирует текст и разметку слов; мышление Кими стримится событиями
    ("think", …) — фронт показывает его от лица Вишенки. Стриминг мышления Яндекс невозможен:
    API Vision его не отдаёт (по ТЗ допустимо стримить только Кими)."""
    kind = kind if kind in WHAT else "ege"
    t0 = time.monotonic()                      # тайминги этапов — одной строкой в конце (essaycheck: проверка …)
    if ocr is None:                            # одним куском: распознавание → анализ (как всегда)
        async for ev, v in _ocr_phase(w, t, kind):
            if ev == "ocr":
                ocr = v
                continue
            yield ev, v
            if ev == "done":                   # работа не распозналась — итог уже отдан
                return
        if ocr is None:
            return
    # ocr задан: распознавание уже было (этап 1), текст мог быть поправлен учеником (apply_text) — анализируем его
    pages, tpages, essay, task = ocr["pages"], ocr["tpages"], ocr["essay"], ocr["task"]
    extra, part, t_ocr = ocr.get("extra") or "", bool(ocr.get("part")), float(ocr.get("t_ocr") or 0.0)
    yield "status", "Проверяю орфографию, пунктуацию, грамматику и речь"   # четыре типа ошибок (ERRORS_PROMPT)
    criteria_text = CRITERIA[kind] + (("\n\nДополнительно — материалы ФИПИ с fipi.ru:\n" + extra[:1500])
                                      if extra else "")   # эталонная таблица всегда в промте, добавка — ограничена
    msg_crit, msg_err = _msg(kind, task, essay, criteria_text), _msg_errors(kind, essay)
    # Два НЕЗАВИСИМЫХ запроса идут ПАРАЛЛЕЛЬНО: ошибки (K2.6 без мышления) и критерии (K2.6 с мышлением).
    # Время проверки — самый долгий из двух, а не сумма; стрим мышления показывается только у критериев.
    q = asyncio.Queue()

    async def _collect(msg, thinking, cap, emit, tag):
        parts, err, c0, tm = [], None, time.monotonic(), {}
        async for ev, v in kimi.ask_stream([{"role": "user", "content": msg}], json_mode=True,
                                           thinking=thinking, max_tokens=cap, tag=tag):
            if ev == "think":                  # стрим мышления Кими — на прелоадер от лица Вишенки
                tm.setdefault("think", time.monotonic() - c0)
                if emit:
                    await q.put(("think", v))
            elif ev == "text":
                tm.setdefault("text", time.monotonic() - c0)
                parts.append(v)
            else:
                err = v
                break
        tm["total"] = time.monotonic() - c0
        return "".join(parts), err, tm

    t_err = asyncio.create_task(_collect(msg_err, False, EC_MAX_ERR, False, "ec-errors"))
    t_crit = asyncio.create_task(_collect(msg_crit, True, EC_MAX_CRIT, True, "ec-criteria"))
    t_crit.add_done_callback(lambda _t: q.put_nowait(None))
    try:
        while True:
            item = await q.get()
            if item is None:
                break
            yield item
        res = await asyncio.gather(t_err, t_crit, return_exceptions=True)
    finally:
        for t in (t_err, t_crit):              # клиент ушёл или сбой — висячих запросов к Кими не оставляем
            if not t.done():
                t.cancel()
    (txt_e, err_e, tm_e), (txt_c, err_c, tm_c) = [r if isinstance(r, tuple) else ("", "net", {}) for r in res]

    async def _json(txt, msg, cap):
        a = _parse_json(txt)
        if not a:                              # один повтор: модель вернула не-JSON
            log.warning("essaycheck: ответ не разобрался как JSON — повтор")
            a = await kimi.ask_json([{"role": "user", "content": msg}], max_tokens=cap)
        return a if isinstance(a, dict) else None

    a_err = None if err_e else await _json(txt_e, msg_err, EC_MAX_ERR)
    a_crit = None if err_c else await _json(txt_c, msg_crit, EC_MAX_CRIT)
    if a_err is None and a_crit is None:
        err = err_c or err_e
        log.warning("essaycheck: анализ не прошёл (%s)", err or "json")
        yield "done", _result({}, essay, task, why=err if err in ("net", "http", "empty") else "net")
        return
    a = {}
    if isinstance(a_err, dict) and "errors" in a_err:
        a["errors"] = a_err["errors"]
    elif isinstance(a_crit, dict) and "errors" in a_crit:      # модель вернула всё одним ответом
        a["errors"] = a_crit["errors"]
    if isinstance(a_crit, dict):
        for k in ("criteria", "recommendations", "flags"):
            if k in a_crit:
                a[k] = a_crit[k]
    if (a_err is None) != (a_crit is None):    # один из двух запросов не дал ответа — честно помечаем
        part = True
        log.warning("essaycheck: получена только часть разбора (ошибки=%s, критерии=%s)",
                    a_err is not None, a_crit is not None)
    if not (a.get("errors") or a.get("criteria")):
        log.warning("essaycheck: разбор пустой — показываю превью с пустыми блоками")
        yield "done", _result(a, essay, task, why="empty")
        return
    for e in _as_list(a.get("errors")):        # пустой src не пропускаем — подставляем шаблон
        if isinstance(e, dict) and not _s(e.get("src")):
            e["src"] = SRC_TEMPLATE
    # координаты ошибок — только из разметки OCR; поиск O(страницы × ошибки × фрагмент)
    # выносится из событийного цикла: иначе на 60 ошибках он морозит бота и API на секунды,
    # HEALTHCHECK (timeout=5s) не проходит и Render перезапускает сервис
    t_bind0 = time.monotonic()
    await asyncio.to_thread(_bind_coords, a, pages)
    t_bind = time.monotonic() - t_bind0
    a["_lang"] = True                          # К7–К10 посчитает _result по числу ошибок
    yield "status", "Сверяю с критериями ФИПИ…"
    res = _result(a, essay, task, why="part" if part else "")
    sc, by_type = res.get("score") or {}, {}
    for e in res.get("errors") or []:
        by_type[e.get("type")] = by_type.get(e.get("type"), 0) + 1
    f = lambda v: "-" if v is None else "%.1fs" % v
    log.info("essaycheck: проверка kind=%s итого=%.1fs | ocr=%.1fs ошибки=%s (1й текст %s) критерии=%s (мышление %s, "
             "1й текст %s) привязка=%.2fs | ошибок=%d %s критериев=%d балл=%s/%s flags=%s why=%s missing=%s",
             kind, time.monotonic() - t0, t_ocr, f(tm_e.get("total")), f(tm_e.get("text")), f(tm_c.get("total")),
             f(tm_c.get("think")), f(tm_c.get("text")), t_bind, len(res.get("errors") or []),
             by_type or "{}", len(res.get("criteria") or []), sc.get("got"), sc.get("max"),
             [k for k, v in (a.get("flags") or {}).items() if v is True] or "-",
             res.get("why") or "-", res.get("missing") or "-")
    yield "done", res
