"""Файлы чата с ИИ: чтение вложений и сборка Word / PDF / PNG (только chatfiles — без сети и aiogram)."""
import base64, io, os, sys, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import chatfiles as cf
def ok(c): print("  ✓", c)
b64 = lambda b: base64.b64encode(b).decode()

title, blocks = cf.parse_doc("# План урока\nВведение\n## Разминка\n- 5 минут\n1. Теория\n**Итог**: всё")
assert title == "План урока" and blocks[1] == ("h", "Разминка") and blocks[2] == ("li", "•", "5 минут") and blocks[3] == ("li", "1.", "Теория") and blocks[4] == ("p", "Итог: всё"); ok("ответ модели разбирается в заголовок, разделы, списки")
assert cf.parse_doc("- один\n- два")[0] == "Ответ"; ok("ответ без заголовка получает «Ответ»")

d = cf.docx_bytes(title, blocks)
assert zipfile.ZipFile(io.BytesIO(d)).testzip() is None and "Теория" in cf.docx_text(d); ok("Word-файл собирается и читается обратно")
p = cf.pdf_bytes(title, blocks)
assert p[:5] == b"%PDF-" and "Теория" in cf.pdf_text(p); ok("PDF собирается с русским текстом")
png = cf.infographic_png(*cf.parse_infographic({"title": "Тема", "items": [{"h": "Раз", "t": "текст " * 40}, {"h": "Два", "t": "ещё"}]}))
assert png[:8] == b"\x89PNG\r\n\x1a\n"; ok("инфографика рисуется в PNG")
for bad in ({}, {"items": []}, {"items": [{}]}, [], None):
    try: cf.parse_infographic(bad); raise SystemExit("не отклонено")
    except ValueError: pass
ok("пустая инфографика отклоняется")

for fmt, res, ext in (("docx", "# Т\nтекст", ".docx"), ("pdf", "# Т\nтекст", ".pdf"), ("image", {"title": "Т", "items": [{"h": "а", "t": "б"}]}, ".png")):
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
print("test_chat_files: OK")
