"""Синтаксис JavaScript страницы кабинета — целиком, по каждому <script>-блоку.

Регресс P0 (9 ноября 2026): в деплой ушла страница с потерянной «}» — блок
`if(nj&&nj.ok){me.native=nj;me.t0=Date.now() // …` не закрывался перед `else if`.
Один сломанный блок убивает ВЕСЬ скрипт страницы (SyntaxError при разборе): бут не
выполняется, Telegram.WebApp.ready() не вызывается — мини-апп вечно висит на
«загрузке 93%». Существующие тесты вырезают из страницы куски функций и этого не
видят; здесь проверяется настоящая страница целиком.

Запуск: python3 tests/test_page_syntax.py  (без node — честный пропуск)
"""
import os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(HERE, "docs", "index.html"), encoding="utf-8").read()

node = shutil.which("node")
if not node:
    print("  - node не найден — синтаксис страницы не проверен (поставьте node.js)")
    sys.exit(0)

blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
assert blocks, "на странице нет ни одного <script> — проверять нечего (сломали разметку?)"

bad = []
for i, b in enumerate(blocks):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(b)
        name = f.name
    r = subprocess.run([node, "--check", name], capture_output=True, text=True)
    os.unlink(name)
    if r.returncode:
        first = next((l for l in r.stderr.splitlines() if l.startswith(("SyntaxError", "Unexpected"))), r.stderr.splitlines()[0])
        bad.append((i, len(b), first))

assert not bad, "сломанные <script>-блоки (страница не загрузится, Telegram будет висеть на загрузке): %s" % bad
print("  ✓ все %d <script>-блоков страницы разбираются без синтаксических ошибок" % len(blocks))
print("test_page_syntax: OK")
