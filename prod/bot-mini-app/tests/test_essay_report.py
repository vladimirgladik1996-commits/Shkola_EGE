"""«ИИ-проверка»: PDF (A4) и Word с разбором, отправка в Telegram по завершении проверки.

Сборка — essayreport (код файлов «Чата с ИИ» + формат A4 из макета). aiohttp/aiogram не нужны:
проверяется модуль сборки и текст подключения в bot.py.
Запуск: python3 tests/test_essay_report.py"""
import io, os, re, sys, zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import essayreport as er


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
print("OK")
