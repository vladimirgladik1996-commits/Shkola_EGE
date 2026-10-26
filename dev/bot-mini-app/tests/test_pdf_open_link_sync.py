"""Открытие файла в «Чате с ИИ»: tg.openLink зовётся СИНХРОННО в обработчике тапа.

Регресс (баг-репорт 013): кнопка «Скачать» и тап по карточке PDF молча не открывали файл.
tg.openLink честно обрабатывается Telegram только в ответ на тап; любой await перед вызовом
(сетевой запрос за свежей ссылкой) переводит вызов вне user-gesture — клиенты его игнорируют.
Отсюда и разница, которую видел пользователь: «Скачать АПК» работал (там openLink без await),
а PDF — нет. Сеть Telegram не используется; проверяем СТРУКТУРУ обработчиков в docs/index.html.
"""
import os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
def ok(c): print("  ✓", c)

html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()


def fn_body(src, name):
    """Тело функции name(...) — от «{» после сигнатуры до парной «}».
    Скобки и строки считаем, чтобы тело не обрезалось на вложенных объектах/шаблонах."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"функция {name}() не найдена в index.html"
    i = src.index("(", m.end() - 1)
    depth, q = 0, None
    while i < len(src):                       # пропускаем список аргументов
        ch = src[i]
        if q:
            if ch == "\\": i += 1
            elif ch == q: q = None
        elif ch in "\"'`": q = ch
        elif ch == "(": depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0: break
        i += 1
    i = src.index("{", i)                     # тело
    start, depth, q = i, 0, None
    while i < len(src):
        ch = src[i]
        if q:
            if ch == "\\": i += 1
            elif ch == q: q = None
        elif ch in "\"'`": q = ch
        elif ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0: return src[start + 1:i]
        i += 1
    raise AssertionError(f"тело {name}() не закрыто")


def code_only(body):
    """Тело без строк и комментариев — чтобы «await» в тексте подсказки не считался вызовом."""
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r"`(?:\\.|[^`])*`", "``", body, flags=re.S)
    body = re.sub(r'"(?:\\.|[^"])*"', '""', body)
    body = re.sub(r"'(?:\\.|[^'])*'", "''", body)
    return body


def first_await(body):
    m = re.search(r"\bawait\b", body)
    return m.start() if m else None


# ---- 1. chOpen: openLink строго ДО первого await -------------------------------------------
body = code_only(fn_body(html, "chOpen"))
assert body.strip(), "chOpen() разобран в пустоту"
ol = body.find("tg.openLink(")
assert ol != -1, "chOpen() больше не зовёт tg.openLink — контракт открытия потерян"
aw = first_await(body)
assert aw is None or ol < aw, (
    "tg.openLink() вызывается ПОСЛЕ await — Telegram молча проигнорирует вызов.\n"
    "  openLink обязан идти синхронно в обработчике тапа; свежую ссылку готовим заранее.\n"
    f"  позиция openLink={ol}, первый await={aw}")
ok("chOpen(): tg.openLink идёт до первого await (user-gesture не теряется)")

# ---- 2. «Скачать» и тап по карточке — один и тот же путь ----------------------------------
dl = code_only(fn_body(html, "chDl"))
assert re.search(r"\bchOpen\s*\(", dl), "chDl() («Скачать») больше не зовёт chOpen — пути разошлись"
ok("chDl() («Скачать») идёт через chOpen() — единый путь открытия")

# ---- 3. «Скачать АПК» остаётся синхронным (регресс-страж) --------------------------------
apk = code_only(fn_body(html, "apkDl"))
m = re.search(r"tg\.openLink\(", apk)
assert m, "apkDl() больше не зовёт tg.openLink"
aw = first_await(apk[:m.start()])
assert aw is None, "apkDl(): tg.openLink ушёл после await — «Скачать АПК» сломается тем же способом"
ok("apkDl(): «Скачать АПК» по-прежнему зовёт tg.openLink синхронно")

# ---- 4. у ссылки PDF есть запасной путь, кроме openLink ------------------------------------
assert "tg.downloadFile" in html or "createElement(\"a\")" in html, \
    "нет запасного пути открытия/скачивания, кроме tg.openLink"
ok("есть запасной путь: downloadFile / <a download>, если openLink недоступен")

print("ALL PDF OPEN-LINK SYNC TESTS PASSED")
