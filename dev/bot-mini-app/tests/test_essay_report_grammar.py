"""Тип ошибки «Грамматика» (К9) сквозной: бэкенд → отчёт (HTML, PDF reportlab, Word) → фронт mini-app.
Запуск: python3 tests/test_essay_report_grammar.py"""
import io, os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="***", LOG_LEVEL="CRITICAL")
sys.path.insert(0, ROOT)
import essaycheck, essayreport as er
def ok(c): print("  ✓", c)
essaycheck.MIN_WORDS = {"ege": 0, "oge": 0}

ESSAY = "Я считаю что книга важна. Он ложит книгу на стол. Автор пишет о о любви. Мы пошли в парк"
ERR = [{"type": "Пунктуация", "found": "считаю что", "correct": "считаю, что", "why": "запятая", "src": "Розенталь, §1"},
       {"type": "Грамматика", "found": "ложит", "correct": "кладёт", "why": "форма глагола", "src": "Лопатин, §2"},
       {"type": "Речь", "found": "о о", "correct": "о", "why": "повтор", "src": "Критерии ФИПИ, К10"},
       {"type": "Орфография", "found": "пошли", "correct": "пошли", "why": "тест", "src": "Розенталь, §3"}]
CR = [{"name": n, "got": g, "max": m, "why": "ок", "tip": ""} for n, g, m in
      (("К1 Отражение позиции автора", 1, 1), ("К2 Комментарий к позиции", 3, 3), ("К3 Собственное отношение", 2, 2),
       ("К4 Фактическая точность", 1, 1), ("К5 Логичность", 2, 2), ("К6 Этические нормы", 1, 1))]
r = essaycheck._result({"_lang": True, "errors": ERR, "criteria": CR, "recommendations": []}, ESSAY, "задание", kind="ege")
assert [e["type"] for e in r["errors"]] == ["Пунктуация", "Грамматика", "Речь", "Орфография"], r["errors"]

# ---------- отчёт ----------
assert er.TYPES == essaycheck.TYPES == ("Орфография", "Пунктуация", "Грамматика", "Речь")
c = er._counts(r)
assert c == {"Орфография": 1, "Пунктуация": 1, "Грамматика": 1, "Речь": 1}, c
html = er.html_report(r, "ege", "Тест")
import json
def _strings(o):
    if isinstance(o, str): yield o
    elif isinstance(o, dict):
        for v in o.values(): yield from _strings(v)
    elif isinstance(o, list):
        for v in o: yield from _strings(v)
blob = json.JSONDecoder().raw_decode(html[html.index("window.REPORT=") + len("window.REPORT="):])[0]   # страницы строит JS из этих блоков
flat = "\n".join(_strings(blob))
tiles = re.search(r'<div class="tiles">(.*?)</div></div>', flat, re.S)
assert tiles and tiles.group(1).count("<b>") == 4 and "грамматика" in tiles.group(1), "четыре плитки статистики"
assert 'class="e gr"' in flat and 'class="pill gr"' in flat and "Грамматика</span>" in flat, "пометка, пилюля и легенда"
css = open(os.path.join(ROOT, "assets", "essay-report.css"), encoding="utf-8").read()
assert "repeat(4,1fr)" in css and ".e.gr{" in css and ".pill.gr{" in css
ok("HTML-отчёт: 4 плитки, разметка .e.gr / .pill.gr, пункт «Грамматика» в легенде, CSS есть")
pdf = er.pdf_report(r, "ege", "Тест")
assert pdf[:5] == b"%PDF-" and len(pdf) > 3000
ok("PDF (reportlab) с четырьмя плитками статистики собирается")
docx = er.docx_report(r, "ege", "Тест")
import zipfile
assert "Грамматика" in zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode("utf-8")
ok("Word: «Грамматика» в статистике ошибок")

# ---------- фронт ----------
for d in ("docs",):
    html_f = open(os.path.join(ROOT, d, "index.html"), encoding="utf-8").read()
    types = re.search(r"const EC_TYPES=\[(.*?)\];", html_f).group(1)
    assert [t.strip('"') for t in types.split(",")] == list(essaycheck.TYPES), types
    for const in ("EC_KIND", "EC_FTAG"):
        body = re.search(r"const %s=\{(.*?)\};" % const, html_f).group(1)
        assert '"Грамматика"' in body, const
    assert 'type=="Грамматика"?"грамм"' in html_f, "строка критерия К9 по категории ошибки"
    assert "grid-template-columns:repeat(4,1fr);gap:6px;margin-top:14px" in html_f, "4 плитки на экране"
ok("mini-app: EC_TYPES совпадает с бэкендом, EC_KIND/EC_FTAG/ecCritOf знают «Грамматику», плиток 4")
print("test_essay_report_grammar: все проверки прошли")
