"""Файлы чата с ИИ: чтение вложений и сборка Word / PDF / PNG (только chatfiles — без сети и aiogram)."""
import base64, io, os, sys, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import chatfiles as cf
def ok(c): print("  ✓", c)
b64 = lambda b: base64.b64encode(b).decode()

title, blocks = cf.parse_doc("# План урока\nВведение\n## Разминка\n- 5 минут\n1. Теория\n**Итог**: всё")
assert title == "План урока" and blocks[1] == ("h", [("Разминка", {})]) and blocks[2] == ("li", "•", [("5 минут", {})]) and blocks[3] == ("li", "1.", [("Теория", {})]) and blocks[4] == ("p", [("Итог", {"b": 1}), (": всё", {})]); ok("ответ модели разбирается в заголовок, разделы, списки и выделение (раны)")
assert cf.parse_doc("- один\n- два")[0] == "Ответ"; ok("ответ без заголовка получает «Ответ»")

# --- проверка файла видит и начало, и КОНЕЦ документа (ответы теста живут в хвосте) ---
assert cf.page_pick(2) == [0, 1] and cf.page_pick(4) == [0, 1, 2, 3], cf.page_pick(4)
assert cf.page_pick(5) == [0, 1, 3, 4] and cf.page_pick(10) == [0, 1, 8, 9], cf.page_pick(10)
ok("страницы для проверки Кими: первые И последние — конец документа не выпадает из проверки")
long_pdf = cf.pdf_bytes("Тест", [("p", [("Вопрос %d: достаточно длинная строка, чтобы страница документа наполнилась." % i, {})]) for i in range(200)])
shots = cf.render_shots("pdf", long_pdf)
assert len(shots) == 4, len(shots)
ok("render_shots длинного документа отдаёт 4 страницы: начало и конец")

# --- пропуски (подчёркивания) и таблицы не срезаются, а доходят до файла ---
bl = cf.parse_doc("# Т\nЗаполни: _____ тут.")[1][0][1]
assert any(st.get("blank") and st.get("u") for _, st in bl), bl; ok("пропуск «_____» сохраняется как подчёркнутое пустое поле (не срезается)")
tb = cf.parse_doc("| Слово | Значение |\n|---|---|\n| дом | жилище |")[1][0]
assert tb[0] == "table" and len(tb[1]) == 2 and [t for t, _ in tb[1][0][0]] == ["Слово"]; ok("таблица «| … |» разбирается в строки-ячейки")
diag = cf.parse_doc("Схема:\n```html\n<html><head><title>Кв</title></head><body><h1>Квадрат</h1></body></html>\n```")[1]
assert diag[0][0] == "img" and diag[0][1][:8] == b"\x89PNG\r\n\x1a\n"; ok("```html```-схема превращается в PNG-картинку для вставки в файл")

d = cf.docx_bytes(title, blocks)
assert zipfile.ZipFile(io.BytesIO(d)).testzip() is None and "Теория" in cf.docx_text(d); ok("Word-файл собирается и читается обратно")
p = cf.pdf_bytes(title, blocks)
assert p[:5] == b"%PDF-" and "Теория" in cf.pdf_text(p); ok("PDF собирается с русским текстом")
HTML = ("<html><head><title>Тема</title><style>body{background-color:#FBF7E9;color:#0E2250}"
        "h1{font-size:24pt}div{background-color:#FFFFFF;border:1pt solid #E9E1C8;padding:10pt}</style></head>"
        "<body><h1>Тема недели</h1><div>Раз — и длинный текст карточки, чтобы проверить переносы строк и русский шрифт.</div>"
        "<div>Два</div></body></html>")
png = cf.html_png(HTML)
assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000; ok("HTML-инфографика конвертируется в PNG")
pdf = cf.html_pdf(HTML)
assert pdf[:5] == b"%PDF-" and cf.pdf_png(pdf)[:8] == b"\x89PNG\r\n\x1a\n"; ok("конвейер картинки разделён на шаги: HTML → PDF → PNG (каждый шаг виден в чате)")
assert cf.image_title(HTML) == "Тема"; ok("заголовок инфографики доступен отдельно — для имени файла")
for badh in ("", "не html вообще", None):
    try: cf.html_pdf(badh); raise SystemExit("не отклонено")
    except ValueError: pass
ok("шаг HTML → PDF отклоняет не-HTML")
try: cf.pdf_png(b"not-a-pdf"); raise SystemExit("не отклонено")
except ValueError: pass
ok("шаг PDF → PNG отклоняет мусор")
assert cf._html_title(HTML) == "Тема" and cf._html_title("<html><body><h2>Без title</h2></body></html>") == "Без title"; ok("заголовок инфографики берётся из <title> или первого заголовка")
for badh in ("", "не html вообще", None, 5, "<html><body>   </body></html>"):
    try: cf.html_png(badh); raise SystemExit("не отклонено")
    except ValueError: pass
ok("пустая или не-HTML инфографика отклоняется")

for fmt, res, ext in (("docx", "# Т\nтекст", ".docx"), ("pdf", "# Т\nтекст", ".pdf"), ("image", "<html><head><title>Т</title></head><body><p>Русский текст ответа</p></body></html>", ".png")):
    data, name, t = cf.build(fmt, res)
    assert data and name.endswith(ext) and t == "Т"
ok("build() отдаёт байты, имя файла и заголовок для всех форматов")

jpg = io.BytesIO()
from PIL import Image
Image.new("RGBA", (3000, 100), (255, 0, 0, 0)).save(jpg, "PNG")
imgs, docs, names = cf.read_files([{"name": "a.png", "mime": "image/png", "data": b64(jpg.getvalue())},
                                   {"name": "n.txt", "mime": "text/plain", "data": b64("привет".encode())},
                                   {"name": "w.docx", "mime": "", "data": b64(d)}, {"name": "p.pdf", "mime": "", "data": b64(p)},
                                   {"name": "x.zip", "mime": "", "data": b64(b"PK")}])
assert len(imgs) == 1 and base64.b64decode(imgs[0])[:2] == b"\xff\xd8" and Image.open(io.BytesIO(base64.b64decode(imgs[0]))).size[0] <= 1600; ok("картинки приводятся к JPEG ≤1600 px")
assert "привет" in docs and "Теория" in docs and "не читается" in docs and names[0] == "a.png"; ok("текст из TXT, DOCX, PDF извлекается; неизвестный тип — честная пометка")
for badv in ("x", [{}], [{"name": "a", "mime": "", "data": "!!"}], [{"name": "a", "mime": "image/png", "data": b64("не картинка".encode())}], [{"name": "a", "mime": "", "data": b64(b"x")}] * 6):
    try: cf.read_files(badv); raise SystemExit("не отклонено")
    except ValueError: pass
ok("мусор во вложениях отклоняется")

# --- PDF из HTML модели и картинки страниц для проверки Кими ---
p2 = cf.html_pdf(HTML)
assert p2[:5] == b"%PDF-" and len(p2) > 3000; ok("HTML модели конвертируется в PDF")
shots = cf.render_shots("pdf", p2)
assert shots and len(shots) <= 3 and all(base64.b64decode(s)[:2] == b"\xff\xd8" for s in shots); ok("страницы PDF рендерятся в картинки для проверки моделью")
assert len(cf.render_shots("image", png)) == 1; ok("инфографика отдаётся на проверку одной картинкой")
data2, name2, t2 = cf.build("pdf", HTML)
assert data2[:5] == b"%PDF-" and name2.endswith(".pdf") and t2 == "Тема"; ok("build(pdf) собирает PDF из HTML модели")
assert cf.build("pdf", "# Т\nтекст")[2] == "Т"; ok("build(pdf) собирает PDF и из обычного текста (без HTML)")
assert cf.build("docx", HTML)[2] == "Тема"; ok("HTML в ответе docx не ломает сборку — текст вытаскивается")
assert cf.render_shots("pdf", b"garbage") == []; ok("битый файл не роняет проверку — просто нет картинок")

# --- картинки из вложений пользователя в файлах (<img src="attach:имя">) ---
solid = io.BytesIO(); Image.new("RGB", (600, 200), (200, 30, 30)).save(solid, "PNG")
bimg = "data:image/png;base64," + b64(solid.getvalue())
out = cf.resolve_attach('<html><body><img src="attach:a.png" width="120"><img src="attach:нет.png"><img src="https://evil/x.png"><p>т</p></body></html>', {"a.png": bimg})
assert bimg in out and "attach:" not in out and "evil" not in out and out.count("<img") == 1; ok("картинки вложений подставляются, чужие ссылки убираются")
p3, _, _ = cf.build("pdf", '<html><head><title>Т</title></head><body><img src="attach:a.png" width="600" height="200"></body></html>', {"a.png": bimg})
assert p3[:5] == b"%PDF-"
im = Image.open(io.BytesIO(base64.b64decode(cf.render_shots("pdf", p3)[0]))).convert("RGB")
red = sum(1 for y in range(0, im.height, 6) for x in range(0, im.width, 6)
          if im.getpixel((x, y))[0] > 120 and im.getpixel((x, y))[0] > im.getpixel((x, y))[1] + 60)
assert red > 20; ok("PDF собирается с картинкой из вложения — она реально видна на странице")

# --- Word содержит подчёркнутые пропуски, таблицы и схемы-картинки; Кими реально видит документ на проверке ---
rich = ("# Т\n1. Пропуск _____ тут.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "```html\n<html><head><title>С</title></head><body><h1>Схема</h1></body></html>\n```")
wd, wn, wt = cf.build("docx", rich)
z = zipfile.ZipFile(io.BytesIO(wd)); doc = z.read("word/document.xml").decode("utf-8", "replace")
assert "<w:u " in doc and "\u00a0" in doc, "пропуск не подчёркнут в Word"
assert "<w:tbl>" in doc, "таблицы нет в Word"
assert "<w:drawing>" in doc and any(n.startswith("word/media/") for n in z.namelist()) and "word/_rels/document.xml.rels" in z.namelist(), "схема не встроена картинкой"
import xml.etree.ElementTree as ET
for part in ("word/document.xml", "[Content_Types].xml", "_rels/.rels", "word/_rels/document.xml.rels"):
    ET.fromstring(z.read(part))
ok("Word: пропуски подчёркнуты, таблицы на месте, схема встроена картинкой, все части валидны")
assert cf.review_shots("docx", wd, rich), "Кими не видит Word-документ на проверке (0 картинок)"
ok("Кими реально отсматривает Word-документ: review_shots даёт картинки для check_render")
print("test_chat_files: OK")
