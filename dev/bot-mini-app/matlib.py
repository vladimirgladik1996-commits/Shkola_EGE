"""Материалы кабинета (раздел «Материалы») для Вишенки: индекс каталогов, тексты вариантов и поиск по ним.

Модуль читает те же файлы, что и раздел «Материалы» мини-аппа (materials/<id>/index.json и
v01…v36.html), никуда не ходит по сети и ничего не знает про aiohttp/aiogram.

Два потребителя:
  • «Чат с ИИ» (assist.chat_stream): запрос пользователя сначала проверяется по материалам —
    и если в каталогах есть нужный вариант, в запрос модели подставляются его исходный текст,
    задания (тест) и текущее сочинение/эссе. Только когда в материалах ничего нет, модель
    отвечает по общим знаниям (то есть «ищет в интернете» — уже после проверки материалов);
  • голосовой виджет Вишенки (assist.handle_page): «найди/открой эссе ОГЭ к варианту 4»
    открывает страницу варианта (или уточняет каталог, если совпадений несколько), поиск
    работает по номерам вариантов, авторам и тексту страниц.

Все три каталога — по сборнику Р.А. Дощинского: ОГЭ (сочинения 13.1–13.3), ОГЭ «до 80 слов»
и ЕГЭ (сочинение, задание 27); в каждом по 36 вариантов.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
MAT_DIR = os.path.join(HERE, "materials")
MAT_VARIANTS = 36

CATS = (
    {"id": "oge-essays-2027", "s": "ОГЭ-2027",
     "t": "ОГЭ-2027: три эссе 13.1–13.3 (полные сочинения) по сборнику Р.А. Дощинского",
     "kind": "oge"},
    {"id": "oge-essays-2027-vishenka", "s": "ОГЭ-2027 (до 80 слов)",
     "t": "ОГЭ-2027: три эссе 13.1–13.3 до 80 слов по сборнику Р.А. Дощинского",
     "kind": "oge80"},
    {"id": "ege-essays-2027", "s": "ЕГЭ-2027",
     "t": "ЕГЭ-2027: сочинение (задание 27) с критериями К1–К10 по сборнику Р.А. Дощинского",
     "kind": "ege"},
)
BY_ID = {c["id"]: c for c in CATS}

TASK_VERBS = ("перепиш", "перепис", "сделай", "сделать", "напиш", "напис", "сократ", "улучш", "передел",
              "исправ", "оцени", "проверь", "провер", "разбер", "объясн", "аргумент", "подготов",
              "по другой структур", "иначе", "другой структур")
OPEN_VERBS = ("открой", "открыть", "открыва", "покажи", "показ", "посмотр", "перейд", "давай посмотр")
FIND_VERBS = ("найди", "найти", "поищи", "где", "какие", "какой", "каких", "у какого", "кто автор", "список", "перечисл")
STOP = set("""и а в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот
от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам
ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз
тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее
сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про
всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более
всегда конечно всю между это как-то какбудто какой-то
найди найти найдите открой открыть откройте покажи показать посмотри перейди давай где какой какие каких какой-нибудь
эссе сочинение сочинения вариант варианта варианте вариантов варианты сборник сборника сборнике страница странице
страницу текст текста тексте задание задания заданий слов слова слово до из по к""".split())

_page_cache = {}          # (cat, n) -> текст страницы
_index_cache = {}         # cat -> [ {n, sub, q} ]


def _norm(s):
    return re.sub(r"[^0-9a-zа-яё]+", " ", (s or "").lower().replace("ё", "е"))


def _tokens(s):
    return [t for t in _norm(s).split() if t]


def variants(cat):
    """[{n, sub, q}] каталога — как в index.json (номер, автор текста, строка для поиска)."""
    if cat not in _index_cache:
        path = os.path.join(MAT_DIR, cat, "index.json")
        try:
            with open(path, encoding="utf-8") as f:
                rows = json.load(f).get("variants") or []
        except (OSError, ValueError):
            rows = []
        _index_cache[cat] = [r for r in rows if isinstance(r, dict) and isinstance(r.get("n"), int)]
    return _index_cache[cat]


def page_text(cat, n):
    """Текст страницы варианта: исходный текст, задания (тест) и текущее сочинение/эссе с критериями.
    Страница — HTML, из неё вырезаются скрипты/стили и теги. Пустая строка — файла нет."""
    key = (cat, n)
    if key not in _page_cache:
        try:
            with open(os.path.join(MAT_DIR, cat, "v%02d.html" % n), encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            raw = ""
        raw = re.sub(r"<(script|style)\b.*?</\1>", " ", raw, flags=re.I | re.S)
        txt = re.sub(r"<[^>]+>", "\n", raw)
        txt = re.sub(r"[ \t\u00a0]+", " ", txt)
        txt = "\n".join(ln.strip() for ln in txt.split("\n") if ln.strip())
        _page_cache[key] = re.sub(r"\n{2,}", "\n", txt)[:20000]
    return _page_cache[key]


def title(cat, n):
    c = BY_ID.get(cat)
    return "%s · вариант %s" % (c["s"] if c else cat, n)


def catalog_line():
    """Одна строка про каталоги — для промтов модели."""
    return "Каталоги раздела «Материалы» (по сборнику Р.А. Дощинского): " + \
           "; ".join("%s — %s" % (c["s"], c["t"].split(": ", 1)[-1]) for c in CATS) + "."


# ---------- разбор запроса ----------
_NUM_RE = re.compile(r"(?:вариант\w*|№\s*|номер\w*)[^\d]{0,24}(\d{1,2})\b|\b(\d{1,2})[^\d]{0,12}(?:-\s*(?:й|ый)\s+)?вариант")
_WORD_NUM = {"перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5}


def parse_query(q):
    """{cats, num, toks} — каталоги, номер варианта и смысловые слова запроса."""
    n = _norm(q)
    if "до 80" in n or "80 слов" in n or "вишенк" in n:
        cats = ["oge-essays-2027-vishenka"]
    elif re.search(r"\bогэ|огз|oge", n):
        cats = ["oge-essays-2027", "oge-essays-2027-vishenka"]
    elif re.search(r"\bегэ|егз|ege", n) or "задани 27" in n or "задание 27" in n:
        cats = ["ege-essays-2027"]
    else:
        cats = []
    num = None
    m = _NUM_RE.search(n)
    if m:
        num = int(m.group(1) or m.group(2))
    else:
        for w, v in _WORD_NUM.items():
            if w in n and "вариант" in n:
                num = v
                break
    if num is not None and not 1 <= num <= MAT_VARIANTS:
        num = None
    toks = [t for t in n.split() if len(t) >= 4 and t not in STOP]
    return {"cats": cats, "num": num, "toks": toks}


def _author_hit(v, toks):
    """Совпадение по автору текста («По А. П. Чехову» / «А. П. Чехову …» в строке поиска)."""
    words = set(_tokens(v.get("sub", "")) + _tokens((v.get("q") or "")[:60]))
    words = {w for w in words if len(w) >= 4}
    return sum(1 for t in toks if any(w.startswith(t[:5]) or t.startswith(w[:5]) for w in words))


def search(q, limit=6):
    """Варианты, подходящие под запрос: [{cat, n, sub, score}]. Номер и автор весят больше текста."""
    p = parse_query(q)
    cats = p["cats"] or [c["id"] for c in CATS]
    toks, num = p["toks"], p["num"]
    phrase = " ".join(toks[:3])
    out = []
    for cat in cats:
        for v in variants(cat):
            score = 0
            if num is not None:
                if v["n"] != num:
                    continue
                score += 100                                  # номер назван точно — это главный критерий
            score += 20 * _author_hit(v, toks)
            hay = _norm((v.get("q") or "") + " " + (v.get("sub") or ""))
            score += sum(2 for t in toks if t in hay)
            if not score and phrase and len(phrase) >= 8:     # поиск по тексту страницы (авторов и заданий)
                if phrase in _norm(page_text(cat, v["n"])):
                    score += 8
            if score:
                out.append({"cat": cat, "n": v["n"], "sub": v.get("sub", ""), "score": score})
    out.sort(key=lambda r: (-r["score"], r["cat"], r["n"]))
    return out[:limit]


def _looks_like(q):
    """Похоже ли, что вопрос про материалы (а не про кабинет или общий вопрос)."""
    n = _norm(q)
    if re.search(r"\bогэ|\bегэ|до 80|80 слов|вишенк|дощинск|сборник", n):
        return True
    if "вариант" in n and parse_query(q)["num"] is not None:
        return True
    return _any_author(q)


def _any_author(q):
    toks = parse_query(q)["toks"]
    if not toks:
        return False
    for cat in [c["id"] for c in CATS]:
        for v in variants(cat):
            if _author_hit(v, toks):
                return True
    return False


def context(query, max_chars=24000):
    """(текст-контекст для модели, найденные варианты). Проверка материалов ДО поиска в интернете.

    Найденный вариант подставляется целиком: исходный текст, задания (тест) и текущее
    сочинение/эссе — модель сначала изучает их и только потом выполняет задачу."""
    matches = search(query, limit=4)
    if not matches:
        if _looks_like(query):
            return ("ПРОВЕРКА РАЗДЕЛА «МАТЕРИАЛЫ»: по запросу «%s» в каталогах (%s) ничего не нашлось.\n"
                    "Если задача учебная и ответа в материалах кабинета нет — ответь по общим знаниям, "
                    "но материалы кабинета не выдумывай." % (query[:120], "; ".join(c["s"] for c in CATS))), []
        return "", []
    top, used = [], set()
    for m in matches:
        if (m["cat"], m["n"]) in used:
            continue
        used.add((m["cat"], m["n"]))
        top.append(m)
        if len(top) >= 2:
            break
    parts = ["МАТЕРИАЛЫ ИЗ РАЗДЕЛА «МАТЕРИАЛЫ» (проверены по каталогам до любого поиска в интернете — "
             "сначала изучи их: исходный текст, задания (тест) и текущее сочинение/эссе варианта):"]
    for m in top:
        parts.append("\n=== %s, вариант %s (%s) ===\n%s" % (BY_ID[m["cat"]]["s"], m["n"], m["sub"] or "без автора",
                                                            page_text(m["cat"], m["n"])))
    if len(top) > 1 and not parse_query(query)["cats"]:
        parts.append("\n(В каталогах несколько подходящих вариантов — если задача относится к одному из них, "
                     "упомяни какой; если выбор неочевиден — коротко уточни у пользователя.)")
    return "\n".join(parts)[:max_chars], top


# ---------- виджет Вишенки: открыть / уточнить / ответить ----------
def _pick(matches, cats_named):
    """(cat, n) из совпадений, если выбор однозначен; иначе None."""
    uniq = {(m["cat"], m["n"]) for m in matches}
    if len(uniq) == 1:
        cat, n = uniq.pop()
        return cat, n
    if not matches:
        return None
    if cats_named and len({m["cat"] for m in matches}) > 1:
        return None                                  # два каталога — надо уточнить
    same_n = {m["n"] for m in matches}
    if len(same_n) == 1 and len({m["cat"] for m in matches}) > 1:
        return None
    top = matches[0]
    return top["cat"], top["n"]


def _cands(matches):
    seen, out = set(), []
    for m in matches:
        k = (m["cat"], m["n"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"cat": m["cat"], "n": m["n"], "sub": m.get("sub", "")})
        if len(out) >= 4:
            break
    return out


def _ask(cands):
    if len(cands) == 2 and {c["cat"] for c in cands} == {"oge-essays-2027", "oge-essays-2027-vishenka"}:
        return ("Вариант %s есть в двух каталогах: «ОГЭ-2027» (эссе 13.1–13.3, полные сочинения) и "
                "«ОГЭ-2027 (до 80 слов)». Какой нужен?" % cands[0]["n"])
    if len(cands) == 2 and {c["cat"] for c in cands} == {"oge-essays-2027", "ege-essays-2027"}:
        return "Нашёл и ОГЭ, и ЕГЭ. Уточните: эссе ОГЭ или сочинение ЕГЭ?"
    return "Нашёл несколько вариантов: " + ", ".join("%s — %s" % (title(c["cat"], c["n"]), c["sub"] or "без автора")
                                                     for c in cands[:3]) + ". Какой открыть?"


def resolve(text):
    """Запрос виджета Вишенки про материалы → dict вида:
      {"kind":"open",   cat, n, reply}      — открыть страницу варианта;
      {"kind":"choose", cands, reply}       — совпадений несколько, нужен выбор каталога/варианта;
      {"kind":"answer", reply}              — ответ текстом (поиск по авторам и тексту);
      {"kind":"task",   context, reply}     — задача по варианту (переписать, разобрать…): контекст для модели;
      None — запрос не про материалы."""
    if not isinstance(text, str) or not text.strip():
        return None
    if not _looks_like(text):
        return None
    p = parse_query(text)
    matches = search(text, limit=6)
    if p["num"] is not None and not matches:
        return {"kind": "answer",
                "reply": "Варианта %s в каталогах нет — в каждом каталоге 36 вариантов (1–36)." % p["num"]}
    verb_task = any(v in _norm(text) for v in TASK_VERBS)
    if verb_task:
        ctx, _top = context(text)
        return {"kind": "task", "context": ctx,
                "reply": "Сейчас изучу материалы и подготовлю ответ."}
    verb_open = any(v in _norm(text) for v in OPEN_VERBS)
    verb_find = any(v in _norm(text) for v in FIND_VERBS)
    cats_named = bool(p["cats"])
    pick = _pick(matches, cats_named)
    if pick and (verb_open or p["num"] is not None or len(matches) == 1):
        cat, n = pick
        return {"kind": "open", "cat": cat, "n": n,
                "reply": "Открываю: %s." % title(cat, n)}
    if matches and (verb_open or verb_find or p["num"] is not None):
        cands = _cands(matches)
        if cats_named and p["num"] is not None and len({c["cat"] for c in cands}) > 1:
            return {"kind": "choose", "cands": cands, "reply": _ask(cands)}
        if verb_open and len(cands) == 1:
            c = cands[0]
            return {"kind": "open", "cat": c["cat"], "n": c["n"], "reply": "Открываю: %s." % title(c["cat"], c["n"])}
        if verb_open or (p["num"] is not None and cats_named):
            return {"kind": "choose", "cands": cands, "reply": _ask(cands)}
        lines = ["Нашёл в материалах:"]
        for c in cands[:5]:
            lines.append("• %s — %s" % (title(c["cat"], c["n"]), c["sub"] or "без автора"))
        lines.append("Скажите «открой вариант %s» — покажу страницу." % cands[0]["n"])
        return {"kind": "answer", "reply": "\n".join(lines)}
    return None


def choose(text, cands):
    """Ответ на уточнение («до 80 слов» / «полные» / «первый»…) → {"kind":"open", …} | None."""
    if not isinstance(text, str) or not cands:
        return None
    n = _norm(text)
    if any(w in n for w in ("отмен", "не надо", "не нужно", "забуд")):
        return {"kind": "answer", "reply": "Хорошо, отменил."}
    pool = list(cands)
    if "до 80" in n or "80 слов" in n or "вишенк" in n:
        pool = [c for c in pool if c["cat"] == "oge-essays-2027-vishenka"] or pool
    elif "полно" in n or "13.1" in n or "13.2" in n or "13.3" in n or "обычн" in n or "не до 80" in n:
        pool = [c for c in pool if c["cat"] != "oge-essays-2027-vishenka"] or pool
    elif re.search(r"\bогэ|огз", n):
        pool = [c for c in pool if c["cat"].startswith("oge")] or pool
    elif re.search(r"\bегэ|егз", n):
        pool = [c for c in pool if c["cat"].startswith("ege")] or pool
    for w, v in _WORD_NUM.items():
        if w in n and "вариант" not in n and 1 <= v <= len(cands):
            return {"kind": "open", "cat": cands[v - 1]["cat"], "n": cands[v - 1]["n"],
                    "reply": "Открываю: %s." % title(cands[v - 1]["cat"], cands[v - 1]["n"])}
    m = _NUM_RE.search(n)
    if m:
        num = int(m.group(1) or m.group(2))
        hit = [c for c in pool if c["n"] == num] or [c for c in cands if c["n"] == num]
        if len(hit) == 1:
            return {"kind": "open", "cat": hit[0]["cat"], "n": hit[0]["n"],
                    "reply": "Открываю: %s." % title(hit[0]["cat"], hit[0]["n"])}
    if len(pool) == 1:
        c = pool[0]
        return {"kind": "open", "cat": c["cat"], "n": c["n"], "reply": "Открываю: %s." % title(c["cat"], c["n"])}
    if len(pool) < len(cands) and pool:
        return {"kind": "choose", "cands": pool, "reply": _ask(pool)}
    return None
