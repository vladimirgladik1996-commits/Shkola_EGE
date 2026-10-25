"""«ИИ-проверка»: PDF (A4) и Word с разбором, отправка в Telegram по завершении проверки.

Сборка — essayreport: основной движок html (макет ai-check-a4-preview.html → Chromium → PDF; Word — те же
страницы картинками) и запасной reportlab. aiohttp/aiogram не нужны: проверяется модуль сборки и текст
подключения в bot.py. Тесты Chromium пропускаются, если playwright/браузера нет (запасной путь проверяется всегда).
Запуск: python3 tests/test_essay_report.py"""
import io, os, re, sys, zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import essayreport as er
er.ENGINE = "reportlab"          # тесты 1–7 — запасной путь; HTML-движок проверяется в блоке 10


def ok(c): print("  ✓", c)


TEXT = ("В тексте автор поднимает проблему влеяния книги. Я считаю что чтение важно.\n\n"
        "Писатель показывает это в течении всего рассказа. Таким образом автор прав.")
ERR = [("Орфография", "влеяния", "влияния"), ("Пунктуация", "считаю что", "считаю, что"),
       ("Орфография", "в течении", "в течение"), ("Пунктуация", "Таким образом автор", "Таким образом, автор")]
R = {"text": TEXT, "task": "", "missing": [], "why": "",
     "errors": [{"i": k + 1, "type": t, "found": f, "correct": c, "why": "Правило <б> & «в»", "src": "Розенталь, §1"}
                for k, (t, f, c) in enumerate(ERR)],
     "criteria": [{"name": "К1 Проблема", "got": 1, "max": 1, "why": "Верно", "tip": ""},
                  {"name": "Орфография", "got": 1, "max": 3, "why": "2 ошибки", "tip": "Корни"}],
     "recommendations": [{"title": "Корни", "text": "Повторите", "badge": "+1 орфография"}],
     "score": {"got": 2, "max": 4, "percent": 50, "level": "Удовлетворительно"}}
NOW = datetime(2026, 10, 25, 14, 30)

# 1. оба формата собираются, имена и подпись
out = er.build(R, "ege", "Анна К.", NOW)
assert set(out) == {"pdf", "docx"}, out.keys()
pdf, pn = out["pdf"]; dx, dn = out["docx"]
assert pdf[:5] == b"%PDF-" and pn.endswith(".pdf") and "2026-10-25" in pn
assert dx[:2] == b"PK" and dn.endswith(".docx")
cap = er.caption(R, "ege", "Анна К.")
assert "Анна К." in cap and "2 из 4" in cap and "Сочинения ЕГЭ".lower() in cap.lower()
ok("PDF и Word собираются; имена с датой, подпись с учеником и баллом")

# 2. PDF — A4, 3 страницы по макету, нумерация «Стр. N из M»
try:
    import pypdfium2 as pdfium
    d = pdfium.PdfDocument(pdf)
    w, h = d[0].get_size()
    assert abs(w - 595.3) < 1 and abs(h - 841.9) < 1, (w, h)
    assert len(d) == 3, len(d)
    t = d[2].get_textpage().get_text_range()
    assert "Стр. 3 из 3" in t and "Критерии ФИПИ" in t and "Рекомендации" in t, t[:200]
    assert "Разбор ошибок" in d[1].get_textpage().get_text_range()
    ok("PDF: A4, 3 страницы (балл и текст / ошибки / критерии и рекомендации), «Стр. N из M»")
except ImportError:
    ok("PDF: pypdfium2 нет — проверка страниц пропущена")

# 3. Word: валидный docx; пометки ошибок в тексте, список ошибок, критерии
z = zipfile.ZipFile(io.BytesIO(dx))
assert z.testzip() is None
x = z.read("word/document.xml").decode()
for s in ("Прогнозный балл", "влияния", "Критерии ФИПИ", "Рекомендации", "[1]", "[4]", "Анна К."):
    assert s in x, s
assert "<w:tbl>" not in x, "таблицы chatfiles в Word пустые (ячейки без w:tc) — отчёт обходится списком"
ok("Word: балл, текст с пометками [N], список ошибок, критерии и рекомендации — без таблиц chatfiles")

# 4. пометки в тексте: ё=е, знаки между словами, без пересечений; выдумка не помечается
sp = er.mark_spans(TEXT, R["errors"])
assert [i for *_, i in sp] == [1, 2, 3, 4], sp
assert er.mark_spans("Всё хорошо", [{"i": 1, "found": "все  хорошо"}]) == [(0, 10, 1)]
assert er.mark_spans("текст", [{"i": 1, "found": "нет такого"}]) == []
assert er.mark_spans("а а", [{"i": 1, "found": "а"}, {"i": 2, "found": "а"}]) == [(0, 1, 1), (2, 3, 2)]
ok("поиск фрагментов: ё=е, пунктуация между словами, повторы — разные места, выдумка не помечается")

# 5. разбор с пустыми блоками (ничего не выдумываем) и без ученика — тоже собирается
for miss in (["text"], ["errors"], ["criteria"], ["recommendations"], ["text", "errors", "criteria", "recommendations"]):
    r = dict(R, missing=miss)
    if "text" in miss: r["text"] = ""
    if "errors" in miss: r["errors"] = []
    if "criteria" in miss: r["criteria"] = []; r["score"] = {"got": 0, "max": 0, "percent": 0, "level": ""}
    if "recommendations" in miss: r["recommendations"] = []
    o = er.build(r, "oge")
    assert set(o) == {"pdf", "docx"}, miss
    assert er.caption(r, "oge")
ok("пустые блоки (text/errors/criteria/recommendations) — отчёт собирается с честной пометкой")

# 6. один формат упал — второй всё равно уходит
orig = er.pdf_report
er.pdf_report = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    assert set(er.build(R, "ege")) == {"docx"}
finally:
    er.pdf_report = orig
ok("сбой одного формата не лишает второго")

# 7. длинный разбор не ломает вёрстку: много ошибок и длинные строки
big = dict(R, errors=[dict(R["errors"][0], i=k + 1, found="влеяния", why="очень длинное правило " * 20) for k in range(60)])
o = er.build(big, "ege", "Имя " * 10)
assert set(o) == {"pdf", "docx"}
ok("60 ошибок с длинными пояснениями собираются (таблица переносится по страницам)")

# 8. подключение в bot.py: фоном, только Telegram-пользователю и только при реальном разборе
src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
assert "essayreport" in src.split("\n")[6], "essayreport не импортирован"
assert re.search(r'_ec_job_end\(job, \{"t": "done", "result": v\}\)\s+ec_report_start\(request\.app\.get\("bot"\), uid, v, kind\)', src)
for s in ("async def ec_report_send(", "def ec_report_start(", "essayreport.build", "send_document(uid, BufferedInputFile(",
          "uid <= 0", 'r.get("errors") or r.get("criteria")', "EC_BG"):
    assert s in src, s
ok("bot.py: после done отчёт собирается и уходит фоном; без Telegram-id и при пустом разборе — не отправляется")

# 9. ассет шапки на месте
assert os.path.getsize(os.path.join(ROOT, "assets", "essay-logo.jpg")) > 1000
ok("assets/essay-logo.jpg (аватар Вишенки из макета A4) на месте")
# 10. HTML-движок: разметка макета, правило критериев как у фронта, раскладка по страницам, Word = страницы A4
assert er._crit_state({"got": 1, "max": 2}) == ("w", "частично"), "1 из 2 — «частично», как в макете и на фронте"
assert er._crit_state({"got": 1, "max": 3}) == ("r", "основная потеря") and er._crit_state({"got": 3, "max": 3}) == ("", "полный балл")
assert er._pct(4) == 70 and er._pct(2) == 35 and er._pct(1) == 18 and er._pct(9) == 100, "ширины полос как на фронте (4/2/1 → 70/35/18)"
h = er.html_report(R, "ege", "Анна К.", NOW)
assert h.startswith("<!DOCTYPE html>") and "window.REPORT=" in h and "</script><script>" in h
blocks = er._blocks(R, "ege", "Анна К.", NOW)
joined = "".join(b["h"] for b in blocks)
for s_ in ('class="hero"', 'class="e"', 'class="e pu"', 'class="num pu"', 'class="pill pu"', 'class="kc"', 'class="kc r"',
           'class="rc"', 'class="note"', "<s>влеяния</s>", "Правило &lt;б&gt; &amp; «в»", "Анна К."):
    assert s_ in joined, s_
assert sum(1 for b in blocks if b["n"]) == 2, "страницы 2 и 3 начинаются с новой страницы"
assert er._blocks(dict(R, missing=["text"], text=""), "ege", "", NOW)
assert 'class="empty"' in "".join(b["h"] for b in er._blocks(dict(R, missing=["text", "criteria"], text="", criteria=[]), "ege", "", NOW))
ok("HTML-разметка по макету: герой, пометки e/pu/re, таблица, критерии w/r, рекомендации; пустые блоки — «empty»")

try:
    import pypdfium2 as pdfium
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        _p.chromium.launch(args=["--no-sandbox"]).close()
    chromium = True
except Exception as e:
    chromium = False
    ok(f"Chromium недоступен ({type(e).__name__}) — проверка HTML-движка пропущена")
if chromium:
    er.ENGINE = "html"
    o = er.build(R, "ege", "Анна К.", NOW)
    assert set(o) == {"pdf", "docx"}
    d = pdfium.PdfDocument(o["pdf"][0])
    assert len(d) == 3 and abs(d[0].get_size()[0] - 595.3) < 1.5 and abs(d[0].get_size()[1] - 841.9) < 1.5
    for k in range(3):
        t = d[k].get_textpage().get_text_range()
        assert f"Стр. {k + 1} из 3" in t and "Школа ЕГЭ · ИИ-проверка" in t, k
    assert "Разбор ошибок" in d[1].get_textpage().get_text_range() and "Критерии ФИПИ" in d[2].get_textpage().get_text_range()
    z = zipfile.ZipFile(io.BytesIO(o["docx"][0]))
    assert z.testzip() is None
    x = z.read("word/document.xml").decode()
    assert x.count("<wp:inline") == 3 and sum(n.startswith("word/media/page") for n in z.namelist()) == 3
    assert 'cx="' in x and "влияния" in x, "текст страниц — в альт-тексте картинок"
    ok("HTML-движок: PDF A4 из макета, 3 страницы «Стр. N из 3»; Word — 3 страницы-картинки с альт-текстом")

    big = dict(R, errors=[dict(R["errors"][0], i=k + 1, found="влеяния", why="очень длинное правило " * 20) for k in range(60)])
    ob = er.build(big, "ege", "Имя " * 10)
    db = pdfium.PdfDocument(ob["pdf"][0])
    n = len(db)
    assert n > 3 and f"Стр. {n} из {n}" in db[n - 1].get_textpage().get_text_range(), n
    assert zipfile.ZipFile(io.BytesIO(ob["docx"][0])).read("word/document.xml").decode().count("<wp:inline") == n
    ok(f"60 ошибок: таблица переносится ({n} стр.), на каждой шапка и «Стр. N из {n}»; страниц в Word столько же")

    # Chromium не запустился → запасной путь: оба файла всё равно уходят
    orig = er.render_pdf
    er.render_pdf = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no chromium"))
    try:
        fb = er.build(R, "ege", "Анна К.", NOW)
    finally:
        er.render_pdf = orig
    assert set(fb) == {"pdf", "docx"} and fb["pdf"][0][:5] == b"%PDF-" and b"<w:tbl>" not in zipfile.ZipFile(io.BytesIO(fb["docx"][0])).read("word/document.xml")
    ok("Chromium упал → PDF (reportlab) и текстовый Word всё равно собираются")
print("OK")
