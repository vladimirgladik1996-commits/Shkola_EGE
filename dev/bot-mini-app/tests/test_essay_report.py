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

# 3. Word — редактируемый документ, собранный кодом: таблицы, абзацы, колонтитулы, ни одной страницы-картинки
from xml.dom import minidom
from xml.sax.saxutils import escape as xesc
z = zipfile.ZipFile(io.BytesIO(dx))
assert z.testzip() is None
for n_ in z.namelist():
    if n_.endswith((".xml", ".rels")):
        minidom.parseString(z.read(n_))                    # весь XML корректен
x = z.read("word/document.xml").decode()
for s in ("Прогнозный балл", "влияния", "Критерии ФИПИ", "Рекомендации", "Анна К.", xesc(R["errors"][0]["why"], {'"': "&quot;"}), R["criteria"][0]["name"]):
    assert s in x, s
assert x.count("<w:tbl>") >= 4 and "<w:tblHeader/>" in x, "настоящие таблицы Word, шапка таблицы ошибок повторяется на страницах"
assert 'w:val="Heading1"' in x and x.count('w:val="Heading2"') == 5, "заголовки — стилями (навигация Word)"
assert "<w:drawing>" not in x and not any(n_.startswith("word/media") for n_ in z.namelist()), "в Word нет ни одной картинки — только текст и таблицы"
for n_ in ("word/styles.xml", "word/settings.xml", "docProps/app.xml", "docProps/core.xml", "word/header1.xml", "word/footer1.xml"):
    assert n_ in z.namelist(), n_
ft = z.read("word/footer2.xml").decode()
assert "PAGE" in ft and "NUMPAGES" in ft and "<w:titlePg/>" in x, "«Стр. N из M» полями Word, первая страница со своей шапкой"
ids = re.findall(r'<wp:docPr id="(\d+)"', x)
assert len(ids) == len(set(ids))
try:
    import docx as pydocx
    dd = pydocx.Document(io.BytesIO(dx))               # строгий разбор пакета
    assert len(dd.tables) >= 4 and sum(p_.style.name == "Heading 2" for p_ in dd.paragraphs) == 5 and dd.paragraphs[0].style.name == "Heading 1"
except ImportError:
    pass
for e_ in R["errors"]:
    assert xesc(e_["found"]) in x and xesc(e_["correct"]) in x, e_      # текст ошибок — обычный текст, а не картинка
ok("Word: настоящие таблицы, стили заголовков, колонтитулы с полями Стр. N из M, XML корректен, текст редактируется")

# 3b. запасной текстовый Word (docx_report) по-прежнему собирается — на него уходит build, если native упадёт
fx = zipfile.ZipFile(io.BytesIO(er.docx_report(R, "ege", "Анна К.", NOW))).read("word/document.xml").decode()
for s in ("Прогнозный балл", "[1]", "[4]", "Анна К."):
    assert s in fx, s
assert "<w:tbl>" not in fx
ok("запасной Word (docx_report): прежний текстовый, без таблиц chatfiles")
real = er.docx_native
er.docx_native = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    fb_ = er.build(R, "ege", "Анна К.", NOW)
finally:
    er.docx_native = real
assert set(fb_) == {"pdf", "docx"} and "[1]" in zipfile.ZipFile(io.BytesIO(fb_["docx"][0])).read("word/document.xml").decode()
ok("native упал → уходит запасной текстовый Word")

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
assert [er._pct(n, 4) for n in (4, 2, 1)] == [70, 35, 18], "как в макете: 4/2/1 → 70/35/18"
for cnts_ in ((8, 7, 6), (12, 9, 3), (6, 6, 6), (5, 3, 0)):
    ws_ = [er._pct(n, max(cnts_)) for n in cnts_]
    assert max(ws_) == 100 or max(cnts_) <= 5, ws_
    assert [w for _, w in sorted(zip(cnts_, ws_))] == sorted(ws_) and len(set(ws_)) == len(set(cnts_)), (cnts_, ws_)   # разные числа — разные полосы
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
    assert z.testzip() is None and "<w:tbl>" in z.read("word/document.xml").decode(), "Word из build — редактируемый"
    pg_ = er.docx_pages(er.pdf_pages_png(o["pdf"][0]), ["текст страницы"] * 3, "t")          # режим ESSAY_DOCX=pages
    px = zipfile.ZipFile(io.BytesIO(pg_)).read("word/document.xml").decode()
    assert px.count("<wp:inline") == 3 and "<w:tbl>" not in px
    ok("HTML-движок: PDF A4 из макета, 3 страницы «Стр. N из 3»; Word — редактируемый; режим pages — 3 страницы-картинки")

    big = dict(R, errors=[dict(R["errors"][0], i=k + 1, found="влеяния", why="очень длинное правило " * 20) for k in range(60)])
    ob = er.build(big, "ege", "Имя " * 10)
    db = pdfium.PdfDocument(ob["pdf"][0])
    n = len(db)
    assert n > 3 and f"Стр. {n} из {n}" in db[n - 1].get_textpage().get_text_range(), n
    bx = zipfile.ZipFile(io.BytesIO(ob["docx"][0])).read("word/document.xml").decode()
    assert bx.count("<w:tr>") > 60 and "<w:tblHeader/>" in bx
    ok(f"60 ошибок: таблица переносится ({n} стр.), на каждой шапка и «Стр. N из {n}»; в Word 60 строк таблицы с повторяющейся шапкой")

    # Chromium не запустился → запасной путь: оба файла всё равно уходят
    orig = er.render_pdf
    er.render_pdf = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no chromium"))
    try:
        fb = er.build(R, "ege", "Анна К.", NOW)
    finally:
        er.render_pdf = orig
    assert set(fb) == {"pdf", "docx"} and fb["pdf"][0][:5] == b"%PDF-" and b"<w:tbl>" in zipfile.ZipFile(io.BytesIO(fb["docx"][0])).read("word/document.xml")
    ok("Chromium упал → PDF (reportlab) и редактируемый Word всё равно собираются")
# 11. кнопки «Переслать PDF/Word» на превью: сервер (нормализация, общий кэш) и экран (общая пересылка чата)
try:
    import aiohttp  # noqa: F401
except ImportError:                       # без aiohttp (минимальное окружение) essaycheck не импортируется — подставляем заглушки
    from unittest.mock import MagicMock
    for m_ in ("aiohttp", "aiohttp.web", "aiohttp.client_exceptions"):
        sys.modules.setdefault(m_, MagicMock())
import essaycheck
srv = essaycheck._result({"errors": [dict(e, x=10.5, y=20, page=0) for e in R["errors"]], "criteria": R["criteria"],
                          "recommendations": R["recommendations"]}, R["text"], "")
cli = {k: srv[k] for k in ("text", "task", "errors", "criteria", "recommendations", "missing", "why", "score")}
cli["errors"] = [{k: e[k] for k in ("i", "type", "found", "correct", "why", "src")} for e in srv["errors"]]   # как ecNrm: без x/y/page
back = er.from_client(cli)
assert back and er.cache_key(42, srv, "ege", "") == er.cache_key(42, back, "ege", ""), "ключ сборки у сервера и у экрана совпадает — файл собирается один раз"
assert er.cache_key(42, back, "ege", "") != er.cache_key(42, back, "ege", "Анна") != er.cache_key(43, back, "ege", "Анна")
for bad_ in (None, [], {"errors": "x"}, {"errors": [], "criteria": []}, {"text": "x" * 100001, "errors": [{"found": "а"}]}):
    assert er.from_client(bad_) is None, bad_
ok("from_client: разбор с экрана нормализуется на сервере; мусор и пустой разбор отклоняются; ключ кэша совпадает с серверным")

bot_src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
for s_ in ("async def ec_report_files(", "def ec_report_links(", "async def api_essay_report(", "essayreport.from_client(",
           'add_post("/api/essay_report/files", api_essay_report)', "chat_file_put(uid, data, name, EC_REPORT_MIME[ext])", "rec[5] = fid"):
    assert s_ in bot_src, s_
assert bot_src.count("essayreport.build") == 1, "сборка отчёта — в одном месте (общий кэш)"
html_src = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
for s_ in ("async function chShareX(x)", "return chShareX(x)}", "function ecRepBox(r)", "ecRepShare('${k}',this)",
           "chShareX({u:x.url,n:x.name,m:x.mime,k:\"out\"})", "/api/essay_report/files", "Переслать PDF", "Переслать Word", "${ecRepBox(r)}"):
    assert s_ in html_src, s_
i_hero, i_box = html_src.index('class="ec-hero2"'), html_src.index("${ecRepBox(r)}")
assert i_box > i_hero and html_src.index("function ecTabSet(t)") > i_box, "кнопки — сразу под блоком прогнозного балла (внизу экрана превью)"
ok("кнопки «Переслать PDF/Word» под блоком балла; пересылка — общий chShareX чата; файлы и кэш — на сервере")

# 12. пуш 5: текст работы в Word настоящим текстом (приложение к страницам-картинкам) + кэш только полных сборок
png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
dxp = er.docx_pages([png, png], ["стр 1", "стр 2"], "Разбор", R)
z2 = zipfile.ZipFile(io.BytesIO(dxp)); assert z2.testzip() is None
x2 = z2.read("word/document.xml").decode()
assert x2.count("<wp:inline") == 2 and "стр 1" in x2, "страницы-картинки с альт-текстом целы"
i2 = x2.index("Приложение: текст работы")
assert i2 > x2.index("<wp:inline"), "приложение — после страниц-картинок"
assert x2.index("влеяния", i2) > i2 and "книги. Я " in x2[i2:] and " чтение важно." in x2[i2:], \
    "текст работы — настоящим текстом Word, а не только альт-текст картинок"
assert "<w:t>[1]</w:t>" in x2 and "<w:t>[4]</w:t>" in x2, "пометки ошибок [N] сохранены и в приложении"
for r0 in (None, dict(R, text="")):
    assert "Приложение" not in zipfile.ZipFile(io.BytesIO(er.docx_pages([png], ["стр"], "Разбор", r0))) \
        .read("word/document.xml").decode(), "без текста работы приложение не добавляем"
ok("Word: после страниц-картинок — приложение с текстом работы настоящим текстом (с пометками [N])")

assert er.complete({"pdf": (b"p", "a.pdf"), "docx": (b"d", "a.docx")})
for bad_ in (None, {}, {"docx": (b"d", "a.docx")}, {"pdf": (b"", "a.pdf"), "docx": (b"d", "a.docx")},
             {"pdf": (b"p", "a.pdf"), "docx": (b"d", "a.docx"), "x": (b"1", "a.x")}):
    assert not er.complete(bad_), bad_
ok("complete(): для кэша годится только полная сборка (оба формата, файлы непустые)")

for s_ in ("essayreport.complete(files)", "for attempt in (1, 2)", "отчёт не собрался tg_id=%s", "(попытка %d/2)"):
    assert s_ in bot_src, s_
assert bot_src.count("EC_REPORTS.pop(key, None)") == 2, \
    "и упавшая, и неполная сборка вычищаются из кэша (отравленный кэш — причина «PDF не доходит»)"
ok("bot.py: упавшая/неполная сборка не кэшируется; доставка с повтором и диагностикой в лог")

# 13. новый дизайн: итоги из данных разбора; Word — текст, а не картинка; текст работы с пометками попадает в PDF и Word целиком
sm = er.summary(R)
assert sm["strong"] == [c["name"] for c in R["criteria"] if c["got"] >= c["max"]]
assert sm["lost"] == sorted(((c["name"], c["max"] - c["got"]) for c in R["criteria"] if c["got"] < c["max"]), key=lambda x: -x[1])
assert [c[0] for c in sm["chips"]] == [er._short(c["name"]) for c in R["criteria"]] and er._short("К2 Комментарий") == "К2" and er._short("Орфография") == "Орфография"
assert er.summary(dict(R, criteria=[], recommendations=[])) == {"chips": [], "strong": [], "lost": [], "next": []}, "без данных — пустые итоги, ничего не домысливается"
hj = "".join(b["h"] for b in er._blocks(R, "ege", "Анна К.", NOW))
for s_ in ('class="chips"', 'class="sum"', "Сильные стороны", "Где теряются баллы", "С чего начать", 'class="st"', 'class="tiles"'):
    assert s_ in hj, s_
assert "class=\"sum\"" not in "".join(b["h"] for b in er._blocks(dict(R, missing=["criteria"], criteria=[]), "ege", "", NOW)), "нет критериев — нет итогов"
ok("итоги (карта баллов, сильные стороны, потери, что делать) строятся только из данных разбора")

segs = [(s_, i) for para in er._paras(R["text"], R["errors"]) for s_, i in para]
glue = lambda t: re.sub(r"\s+", "", t)
assert glue("".join(s_ for s_, _ in segs)) == glue(R["text"]), "разбор текста по пометкам ничего не теряет"
wx = zipfile.ZipFile(io.BytesIO(er.docx_native(R, "ege", "Анна К.", NOW)))
wdoc = wx.read("word/document.xml").decode()
wtext = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", wdoc))
for s_, _ in segs:
    assert glue(xesc(s_)) in glue(wtext), s_
for s_ in ("Сильные стороны", "Где теряются баллы", "С чего начать", R["criteria"][0]["name"], R["recommendations"][0]["title"]):
    assert xesc(s_) in wdoc, s_
assert len(wtext) > 2 * len(R["text"]), "Word — текст: помимо самой работы разбор, таблицы, критерии"
assert "<w:drawing>" not in wdoc, "в теле Word нет картинок"
assert not any(n_.startswith("word/media") for n_ in wx.namelist()), "в пакете Word нет картинок"
nested = re.findall(r"<w:tc>(?:(?!</w:tc>).)*?<w:tbl>", wdoc, re.S)
assert not nested, "в Word нет вложенных таблиц (мобильные просмотрщики рисуют их плохо)"
fills = set(re.findall(r'w:fill="([0-9A-Fa-f]{6})"', wdoc))
assert fills <= {"8794C0", "EEF0F6", "F5F6FA", "EDF0F8", "FFFFFF"}, f"Word лёгкий: заливки только у тонких полос статистики (светлая заливка и дорожка): {fills}"
ok("Word лёгкий и плоский: без вложенных таблиц, без картинок, заливки только у тонких полос")
ok(f"Word: текст работы с пометками — настоящий текст ({len(wtext)} знаков), ни одной картинки")
if chromium:
    er.ENGINE = "html"
    pdf_ = er.build(R, "ege", "Анна К.", NOW)["pdf"][0]
    dd_ = pdfium.PdfDocument(pdf_)
    ptext = glue("".join(dd_[k].get_textpage().get_text_range() for k in range(len(dd_))))
    for s_, _ in segs:
        assert glue(s_) in ptext, s_
    for s_ in ("Сильные стороны", "Гдетеряютсябаллы", "Счегоначать", glue(R["criteria"][0]["name"])):
        assert glue(s_).lower() in ptext.lower(), s_          # заголовки карточек набраны капсом (text-transform) — регистр не важен
    ok("PDF: текст работы с пометками и итоги — текстовый слой (выделяется и ищется), не картинка")

# 14. строгий порядок элементов OOXML во всех частях Word (мобильные просмотрщики строже Word) и согласованность сеток таблиц
ORD_ = {"pPr": ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "widowControl", "numPr", "pBdr", "shd", "tabs", "spacing", "ind", "jc", "outlineLvl", "rPr"],
        "rPr": ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "color", "spacing", "sz", "szCs", "highlight", "u", "bdr", "shd", "vertAlign", "lang"],
        "tcPr": ["tcW", "gridSpan", "vMerge", "tcBorders", "shd", "noWrap", "tcMar", "vAlign"],
        "tblPr": ["tblStyle", "tblW", "jc", "tblCellSpacing", "tblInd", "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook"],
        "pBdr": ["top", "left", "bottom", "right"], "tcBorders": ["top", "left", "bottom", "right"], "tblBorders": ["top", "left", "bottom", "right", "insideH", "insideV"]}
big_ = dict(R, errors=[dict(R["errors"][k % len(R["errors"])], i=k + 1) for k in range(40)])
for r_, k_, st_ in ((R, "ege", "Анна К."), (big_, "ege", "Имя"), (dict(R, missing=["errors", "recommendations"], errors=[], recommendations=[]), "oge", ""),
                    (dict(R, missing=["text", "criteria"], text="", criteria=[]), "ege", "")):
    zz = zipfile.ZipFile(io.BytesIO(er.docx_native(r_, k_, st_, NOW)))
    for part in ("word/document.xml", "word/header1.xml", "word/header2.xml", "word/footer1.xml", "word/footer2.xml", "word/styles.xml"):
        dom = minidom.parseString(zz.read(part))
        for el in dom.getElementsByTagName("*"):
            if el.prefix == "w" and el.localName in ORD_:
                kids = [c.localName for c in el.childNodes if c.nodeType == 1]
                assert all(k in ORD_[el.localName] for k in kids), (part, el.localName, kids)
                idx = [ORD_[el.localName].index(k) for k in kids]
                assert idx == sorted(idx), (part, el.localName, kids)
        for tbl in dom.getElementsByTagName("w:tbl"):
            ng = len(tbl.getElementsByTagName("w:gridCol"))
            for tr in [c for c in tbl.childNodes if c.nodeType == 1 and c.localName == "tr"]:
                n_ = 0
                for tc in [c for c in tr.childNodes if c.nodeType == 1 and c.localName == "tc"]:
                    gs = [g for g in tc.getElementsByTagName("w:gridSpan") if g.parentNode.parentNode is tc]
                    n_ += int(gs[0].getAttribute("w:val")) if gs else 1
                    assert [c for c in tc.childNodes if c.nodeType == 1][-1].localName == "p", "ячейка заканчивается абзацем"
                assert n_ == ng, (part, "строка не совпала с сеткой", n_, ng)
ok("Word: порядок элементов OOXML и сетки таблиц корректны во всех частях и на четырёх наборах данных")

# 15. устойчивость к просмотрщикам: «точная высота» строки/абзаца игнорируется Google Docs, WPS и мобильными — полосы разбухают,
#     промежутки раздуваются. В Word нет ни одной точной высоты; тонкость обеспечивает шрифт метки абзаца 1 пт.
for r_, k_, st_ in ((R, "ege", "Анна К."), (big_, "ege", "Имя"), (dict(R, missing=["text", "criteria"], text="", criteria=[]), "ege", "")):
    zz = zipfile.ZipFile(io.BytesIO(er.docx_native(r_, k_, st_, NOW)))
    for part in ("word/document.xml", "word/header1.xml", "word/header2.xml"):
        t_ = zz.read(part).decode()
        assert 'lineRule="exact"' not in t_ and 'hRule="exact"' not in t_, (part, "точная высота")
assert '<w:sz w:val="2"/>' in zipfile.ZipFile(io.BytesIO(er.docx_native(R, "ege", "", NOW))).read("word/document.xml").decode()
ok("Word не зависит от «точной высоты»: тонкие полосы и промежутки держит шрифт 1 пт, строки таблиц — «не меньше»")
print("OK")
