"""Контракт разметки: JS-комментарии не попадают в innerHTML.

В шаблонах обратных кавычек (`…`) обычные комментарии `/* … */` и `// …` — это НЕ комментарии,
а самый обычный текст: он попадает в innerHTML и отрисовывается на экране как есть.
Ровно так на экране разбора появилась строка

    /* пустой разбор нечего сохранять — историю не мусорим */

(баг-репорт 009): комментарий стоял внутри шаблона `ecResult()`, в конце строки с
`${…}`. Пользователь видел кусок исходного кода вместо интерфейса.

Тест разбирает все <script> блоки docs/index.html, отслеживает состояние строк/шаблонов
(кавычки, обратные кавычки, литералы регулярок, вложенные `${…}`) и требует, чтобы внутри
«текстовой» части шаблона — той, что попадает в innerHTML — не было ни одного комментария.

Запуск: python3 tests/test_frontend_comments.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

HTML = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()


def ok(c):
    print("  ✓", c)


def script_blocks(html):
    """Куски JS из всех <script>…</script> вместе с номером первой строки в файле."""
    return [(html.count("\n", 0, m.start()) + 1, m.group(1))
            for m in re.finditer(r"<script\b[^>]*>([\s\S]*?)</script>", html, re.I)]


# Символы, после которых «слэш-звёздочка» продолжает слово/атрибут, а не начинает комментарий:
#   image/*  (атрибут accept), https://  (URL) и т.п.
NOT_A_COMMENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$\"'`=:/.-")

# После этих слов «/» начинает литерал регулярки, а не деление: return /x/.test(v)
REGEX_AFTER = {"return", "typeof", "instanceof", "in", "of", "new", "delete",
               "void", "case", "do", "else", "yield", "await", "throw"}
WORD_CHAR = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")


def scan(code):
    """(хиты, синхронизирован ли разбор).

    Хит — комментарий, оказавшийся в текстовой части template literal: именно она
    попадает в innerHTML и видна на экране."""
    hits = []
    mode = "code"            # code | squote | dquote | tmpl | regex | linec
    stack = []               # "tmpl" (текст шаблона) | "expr" (выражение в ${…})
    i, n = 0, len(code)
    lineno = lambda pos: code.count("\n", 0, pos) + 1   # номер строки по позиции — считается только при хите
    word = ""                # копится идентификатор/число в code mode
    prev_value = False       # предыдущий значимый токен закончил выражение (имя, число, ) ] } …)

    def fold():
        """Сложить накопленное слово в признак «выражение закончилось»."""
        nonlocal word, prev_value
        if word:
            prev_value = word.lower() not in REGEX_AFTER
            word = ""

    while i < n:
        c, nxt = code[i], code[i + 1] if i + 1 < n else ""
        if c == "\n":
            if mode == "linec":
                mode = "code"
            i += 1
            continue
        if mode == "linec":
            i += 1
            continue
        if mode == "regex":
            if c == "\\":
                i += 2
                continue
            if c == "[":
                j = i + 1
                while j < n and code[j] != "]":
                    j += 2 if code[j] == "\\" else 1
                i = j + 1
                continue
            if c == "/":
                mode, prev_value, i = "code", True, i + 1
                while i < n and code[i] in "dgimsuvy":
                    i += 1
                continue
            i += 1
            continue
        if mode in ("squote", "dquote"):
            if c == "\\":
                i += 2
                continue
            if c == ("'" if mode == "squote" else '"'):
                mode, prev_value = "code", True
            i += 1
            continue
        if mode == "tmpl":
            if c == "\\":
                i += 2
                continue
            if c == "`":
                stack.pop()
                mode, prev_value, i = "code", True, i + 1
                continue
            if c == "$" and nxt == "{":
                stack.append("expr")
                mode, i = "code", i + 2
                continue
            if c == "/" and nxt in "/*" and (i == 0 or code[i - 1] not in NOT_A_COMMENT_START):
                end = code.find("*/", i + 2) if nxt == "*" else code.find("\n", i)
                hits.append((lineno(i), code[i:end if end >= 0 else n].strip()[:90]))
                i = (end + 2) if (nxt == "*" and end >= 0) else i + 2
                continue
            i += 1
            continue

        # mode == "code"
        if c in WORD_CHAR:
            word += c
            i += 1
            continue
        fold()
        if c == "/" and nxt == "/":
            mode, i = "linec", i + 2
            continue
        if c == "/" and nxt == "*":
            end = code.find("*/", i + 2)
            i = (end + 2) if end >= 0 else n
            continue
        if c == "/":
            mode = "regex" if not prev_value else "code"
            prev_value = True
            i += 1
            continue
        if c == "'":
            mode = "squote"
        elif c == '"':
            mode = "dquote"
        elif c == "`":
            stack.append("tmpl")
            mode = "tmpl"
        elif c == "}" and stack and stack[-1] == "expr":
            stack.pop()
            mode = "tmpl"
            prev_value = True
        elif c in ")]}" or c.isdigit():
            prev_value = True
        elif not c.isspace():
            prev_value = False      # оператор, запятая, точка с запятой…
        i += 1
    fold()
    return hits, (mode == "code" and not stack)


blocks = script_blocks(HTML)
assert blocks, "в docs/index.html не найдено ни одного <script> — разбирать нечего"

bad = []
for first_line, code in blocks:
    hits, in_sync = scan(code)
    assert in_sync, (
        f"index.html:{first_line}: разбор JS сошёлся в несогласованном состоянии — "
        "скрипт не распознан, проверка ненадёжна")
    bad += [f"index.html:{first_line + ln - 1}: {t}" for ln, t in hits]

assert not bad, (
    "JS-комментарии внутри шаблонов обратных кавычек — они отрисуются на экране как текст "
    "(баг-репорт 009). Вынесите их из шаблона:\n" + "\n".join(bad))
ok(f"в {len(blocks)} блоках <script> нет комментариев внутри шаблонов — в разметку не попадает лишний текст")

# Точечная страховка на конкретный регресс: строка с `${…ecStuBlock()}` в шаблоне ecResult()
# обязана заканчиваться переводом строки, а не хвостовым комментарием.
m = re.search(r"\$\{[^}\n]*ecStuBlock\(\)\}([^\n]*)", HTML)
assert m, "в шаблоне ecResult() не найдена строка с ecStuBlock() — проверьте, что экран разбора не переписан"
assert not m.group(1).strip(), (
    "после ${…ecStuBlock()} снова стоит хвостовой текст — он попадёт в innerHTML и будет "
    "виден на экране (баг-репорт 009): " + repr(m.group(1)))
ok("строка с ecStuBlock() в шаблоне ecResult() чистая — регресс баг-репорта 009 закрыт")
