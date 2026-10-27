"""Голосовая навигация по «Материалам»: «Открой страницу эссе 13.2 для варианта 7 ОГЭ» → какой файл открыть.

Модуль без зависимостей (ни bot, ни aiohttp): разбор правилами, без модели — работает и без KIMI_API_KEY,
а результат всегда из белого списка (каталог из CATS, вариант 1…36, раздел из PARTS), что бы ни распознал Whisper.

parse(text)            → то, что удалось услышать: {"cat", "n", "part", "rel"} (только найденное)
resolve(found, ctx)    → ("ok", {"cat","n","part"}) | ("ask", вопрос) | ("err", сообщение)
describe(target)       → «ОГЭ · вариант 7 · эссе 13.2» для ответа Вишенки
"""
import re

CATS = {                                    # id каталога (как в bot.MATERIALS) → подпись
    "oge-essays-2027": "ОГЭ",
    "oge-essays-2027-vishenka": "ОГЭ (до 80 слов)",
    "ege-essays-2027": "ЕГЭ",
}
OGE, OGE80, EGE = "oge-essays-2027", "oge-essays-2027-vishenka", "ege-essays-2027"
VARIANTS = 36
PARTS = {                                   # id вкладки на странице варианта → как назвать вслух
    "p-text": "исходный текст",
    "p-essay": "сочинение (задание 27)",
    "p-131": "эссе 13.1", "p-132": "эссе 13.2", "p-133": "эссе 13.3",
}
PARTS_OF = {OGE: ("p-text", "p-131", "p-132", "p-133"), OGE80: ("p-text", "p-131", "p-132", "p-133"),
            EGE: ("p-text", "p-essay")}

# ---------- слова → числа (голос иногда даёт «седьмого варианта» вместо «7») ----------
_UNITS = {"один": 1, "одна": 1, "одного": 1, "первый": 1, "первого": 1, "первому": 1, "первом": 1, "первое": 1, "первая": 1,
          "два": 2, "две": 2, "второй": 2, "второго": 2, "второму": 2, "втором": 2, "второе": 2, "вторая": 2,
          "три": 3, "третий": 3, "третьего": 3, "третьему": 3, "третьем": 3, "третье": 3, "третья": 3,
          "четыре": 4, "четвертый": 4, "четвертого": 4, "четвертому": 4, "четвертом": 4,
          "пять": 5, "пятый": 5, "пятого": 5, "пятому": 5, "пятом": 5,
          "шесть": 6, "шестой": 6, "шестого": 6, "шестому": 6, "шестом": 6,
          "семь": 7, "седьмой": 7, "седьмого": 7, "седьмому": 7, "седьмом": 7,
          "восемь": 8, "восьмой": 8, "восьмого": 8, "восьмому": 8, "восьмом": 8,
          "девять": 9, "девятый": 9, "девятого": 9, "девятому": 9, "девятом": 9}
_TEENS = {"десять": 10, "десятый": 10, "десятого": 10, "десятому": 10, "десятом": 10,
          "одиннадцать": 11, "одиннадцатый": 11, "одиннадцатого": 11, "одиннадцатому": 11,
          "двенадцать": 12, "двенадцатый": 12, "двенадцатого": 12, "двенадцатому": 12,
          "тринадцать": 13, "тринадцатый": 13, "тринадцатого": 13, "тринадцатому": 13,
          "четырнадцать": 14, "четырнадцатый": 14, "четырнадцатого": 14, "четырнадцатому": 14,
          "пятнадцать": 15, "пятнадцатый": 15, "пятнадцатого": 15, "пятнадцатому": 15,
          "шестнадцать": 16, "шестнадцатый": 16, "шестнадцатого": 16, "шестнадцатому": 16,
          "семнадцать": 17, "семнадцатый": 17, "семнадцатого": 17, "семнадцатому": 17,
          "восемнадцать": 18, "восемнадцатый": 18, "восемнадцатого": 18, "восемнадцатому": 18,
          "девятнадцать": 19, "девятнадцатый": 19, "девятнадцатого": 19, "девятнадцатому": 19}
_TENS = {"двадцать": 20, "двадцатый": 20, "двадцатого": 20, "двадцатому": 20,
         "тридцать": 30, "тридцатый": 30, "тридцатого": 30, "тридцатому": 30}
_ALL = {**_UNITS, **_TEENS, **_TENS}


def _norm(s):
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'!?;:()]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _words_num(tokens, i):
    """Число словами с позиции i → (значение, сколько слов съели) | None: «семь», «двадцать пятого»."""
    if i >= len(tokens):
        return None
    w = tokens[i]
    if w in _TENS:
        if i + 1 < len(tokens) and tokens[i + 1] in _UNITS and _UNITS[tokens[i + 1]] <= 9:
            return _TENS[w] + _UNITS[tokens[i + 1]], 2
        return _TENS[w], 1
    if w in _TEENS:
        return _TEENS[w], 1
    if w in _UNITS:
        return _UNITS[w], 1
    return None


_VARIANT_WORD = r"вариант\w*"
_RE_VAR_DIG = re.compile(r"(?:" + _VARIANT_WORD + r")\s*(?:номер|№|n|#)?\s*(\d{1,2})(?!\s*[.,]\s*\d)\b")
_RE_DIG_VAR = re.compile(r"\b(\d{1,2})\s*-?\s*(?:й|го|му|м)?\s+" + _VARIANT_WORD)
_RE_VAR_ANY = re.compile(r"\b" + _VARIANT_WORD)

_RE_OGE = re.compile(r"(?<![а-я])(?:огэ|ого|о[\s\-.]*г[\s\-.]*э|oge)(?![а-я])")
_RE_EGE = re.compile(r"(?<![а-я])(?:егэ|е[\s\-.]*г[\s\-.]*э|ege)(?![а-я])")
_RE_80 = re.compile(r"(?:до\s*)?(?:80|восьмидесяти)\s*слов|вишенк\w*\s+80|короткие эссе")

_RE_13X = re.compile(r"(?<!\d)(?:13|тринадцать)\s*(?:[.,]|точка|запятая|пункт|\s)\s*(1|2|3|один|два|три|первое|второе|третье)(?!\d)")
_RE_13_BARE = re.compile(r"(?<!\d)(?:13|тринадцать)(?!\d)")
_RE_ESSAY_N = re.compile(r"(?:эссе|сочинени\w*)\s*(?:номер|№)?\s*(1|2|3|один|два|три)\b")
_RE_NTH_ESSAY = re.compile(r"\b(перв\w+|втор\w+|трет\w+)\s+(?:эссе|сочинени\w*)")
_RE_27 = re.compile(r"(?<!\d)(?:27|двадцать седьмо\w+|двадцать семь)(?!\d)")
_RE_TEXT = re.compile(r"\b(?:исходн\w+\s+текст\w*|текст\w*)\b")
_RE_ESSAY_WORD = re.compile(r"\b(?:эссе|сочинени\w*)\b")
_RE_ABOUT = re.compile(r"о каталоге|про каталог|критери\w+|источник\w+|о сборнике")
_RE_NEXT = re.compile(r"\b(?:следующ\w+|дальше|еще один|ещё один)\b")
_RE_PREV = re.compile(r"\b(?:предыдущ\w+|прошл\w+|назад)\b")

_OPEN = re.compile(r"\b(?:открой\w*|открыть|покажи\w*|показать|перейди|перейти|найди\w*|найти|загрузи\w*|"
                   r"запусти\w*|давай|хочу|нужн\w+|вызови|выведи|включи\w*|на страниц\w+|страниц\w+)\b")
_QUESTION = re.compile(r"\b(?:объясни\w*|расскажи\w*|почему|зачем|как|что такое|напиши\w*|проверь|исправь\w*|"
                       r"разбери\w*|сколько|кто|когда|где|придумай\w*|перепиши\w*|помоги\w*)\b")

_ESSAY_IDX = {"1": "p-131", "один": "p-131", "первое": "p-131", "2": "p-132", "два": "p-132", "второе": "p-132",
              "3": "p-133", "три": "p-133", "третье": "p-133"}


def _essay_part(tok):
    tok = tok.strip()
    if tok in _ESSAY_IDX:
        return _ESSAY_IDX[tok]
    for stem, part in (("перв", "p-131"), ("втор", "p-132"), ("трет", "p-133")):
        if tok.startswith(stem):
            return part
    return None


def parse(text):
    """Что услышали в тексте. Возвращает только найденное: cat, n (1…36 | 'about'), part, rel (+1 | −1)."""
    t = _norm(text)
    f = {}
    if not t:
        return f
    # --- каталог ---
    if _RE_80.search(t):
        f["cat"] = OGE80
    elif _RE_OGE.search(t):
        f["cat"] = OGE
    elif _RE_EGE.search(t):
        f["cat"] = EGE
    # --- раздел страницы ---
    m = _RE_13X.search(t)
    t_wo = t
    if m:
        f["part"] = _essay_part(m.group(1))
        t_wo = t[:m.start()] + " " + t[m.end():]
    else:
        m = _RE_NTH_ESSAY.search(t) or _RE_ESSAY_N.search(t)
        if m and not (m.re is _RE_ESSAY_N and _RE_27.search(t)):
            part = _essay_part(m.group(1))
            if part:
                f["part"] = part
                t_wo = t[:m.start()] + " " + t[m.end():]
        if "part" not in f:
            if _RE_27.search(t):
                f["part"] = "p-essay"
            elif _RE_TEXT.search(t):
                f["part"] = "p-text"
            elif _RE_ESSAY_WORD.search(t) and f.get("cat") == EGE:
                f["part"] = "p-essay"
    # --- номер варианта ---
    n = None
    m = _RE_VAR_DIG.search(t_wo) or _RE_DIG_VAR.search(t_wo)
    if m:
        n = int(m.group(1))
    else:
        toks = t_wo.split()
        for i, w in enumerate(toks):
            if re.fullmatch(_VARIANT_WORD, w):
                for j in (i + 1, i - 2, i - 1):                    # «вариант семь» | «седьмого варианта» | «двадцать пятого варианта»
                    if 0 <= j < len(toks):
                        r = _words_num(toks, j)
                        if r and (j + r[1] == i or j > i):
                            n = r[0]
                            break
                if n is not None:
                    break
        if n is None:                                              # «для варианта для 7 ОГЭ» — голое число рядом с каталогом
            m = re.search(r"(?<![\d.,])(\d{1,2})(?![\d.,])", t_wo)
            if m and ("cat" in f or _RE_VAR_ANY.search(t)) and int(m.group(1)) not in (13, 27, 80):
                n = int(m.group(1))
    if n is not None:
        f["n"] = n
    if "n" not in f and _RE_ABOUT.search(t):
        f["n"] = "about"
    if "n" not in f and "part" not in f:
        if _RE_NEXT.search(t) and _RE_VAR_ANY.search(t):
            f["rel"] = 1
        elif _RE_PREV.search(t) and _RE_VAR_ANY.search(t):
            f["rel"] = -1
    elif "n" not in f and _RE_NEXT.search(t) and _RE_VAR_ANY.search(t):
        f["rel"] = 1
    elif "n" not in f and _RE_PREV.search(t) and _RE_VAR_ANY.search(t):
        f["rel"] = -1
    return f


def is_nav(text, found, has_prev=False):
    """Это просьба открыть страницу, а не вопрос по теме? Глагол «открой…» либо короткая фраза-адрес."""
    t = _norm(text)
    if not found:
        return False
    if _OPEN.search(t) and not _QUESTION.search(t):
        return True
    if "rel" in found and len(t.split()) <= 5 and not _QUESTION.search(t):
        return True                                                # «следующий вариант»
    if has_prev and len(t.split()) <= 6 and not _QUESTION.search(t):
        return True                                                # ответ на уточняющий вопрос: «ОГЭ», «седьмой»
    return len(t.split()) <= 7 and not _QUESTION.search(t) and ("n" in found or "part" in found) and \
        ("cat" in found or bool(_RE_VAR_ANY.search(t)))


def merge(prev, found):
    out = dict(prev or {})
    out.update({k: v for k, v in (found or {}).items() if v is not None})
    return out


def describe(tg):
    cat = CATS.get(tg["cat"], "")
    if tg["n"] == "about":
        return f"{cat} · о каталоге"
    s = f"{cat} · вариант {tg['n']}"
    if tg.get("part") and tg["part"] != "p-text":
        s += " · " + PARTS[tg["part"]]
    elif tg.get("part") == "p-text":
        s += " · исходный текст"
    return s


def resolve(found, ctx=None, here=True):
    """found + контекст страницы → цель. ctx: {cat, variant, part} того, что сейчас открыто в «Материалах».
    here=False — мы не в «Материалах»: опираться на контекст нельзя, каталог и вариант надо назвать."""
    ctx = ctx or {}
    f = dict(found or {})
    cat, n, part = f.get("cat"), f.get("n"), f.get("part")
    ctx_cat = ctx.get("cat") if here and ctx.get("cat") in CATS else None
    ctx_n = ctx.get("variant") if here else None
    if isinstance(ctx_n, bool) or not isinstance(ctx_n, int):
        ctx_n = None
    # каталог: назван → по разделу (13.x → ОГЭ, 27 → ЕГЭ) → открытый сейчас
    if not cat and part:
        if part in ("p-131", "p-132", "p-133"):
            cat = ctx_cat if ctx_cat in (OGE, OGE80) else OGE
        elif part == "p-essay":
            cat = EGE
    if not cat:
        cat = ctx_cat
    if not cat:
        return "ask", "Какой каталог открыть: ОГЭ или ЕГЭ?", "cat"
    # номер варианта: назван → «следующий/предыдущий» → открытый сейчас
    if n is None and f.get("rel") and ctx_n:
        n = ctx_n + f["rel"]
        if not 1 <= n <= VARIANTS:
            return "err", f"Вариантов всего {VARIANTS}: дальше в эту сторону страницы нет.", "rel"
    if n is None and ctx_n and (ctx_cat == cat or not ctx_cat):
        n = ctx_n
    if n is None:
        return "ask", "Какой вариант открыть — от 1 до 36?", "n"
    if n != "about" and not (isinstance(n, int) and 1 <= n <= VARIANTS):
        return "err", f"Вариант {n} не нашёл — в каталоге варианты с 1 по {VARIANTS}.", "n"
    if n == "about":
        return "ok", {"cat": cat, "n": "about", "part": None}
    if part and part not in PARTS_OF[cat]:
        if part == "p-essay":
            return "err", "В ОГЭ нет задания 27 — там эссе 13.1, 13.2 и 13.3. Какое открыть?", "part"
        return "err", "В ЕГЭ нет эссе 13.1–13.3 — там исходный текст и сочинение (задание 27). Что открыть?", "part"
    return "ok", {"cat": cat, "n": n, "part": part or "p-text"}
