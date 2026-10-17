"""Файлы для «Чата с ИИ»: чтение вложений мини-аппа и сборка ответа в Word / PDF / картинку.

Зависимости: stdlib + reportlab (Pillow ставится вместе с ним) + pypdf + xhtml2pdf (HTML → PDF для
инфографики) + pypdfium2 (PDF → PNG). Ничего не знает про aiohttp/aiogram, поэтому проверяется
отдельным тестом (tests/test_chat_files.py). Любая ошибка разбора вложения — ValueError
(бот ответит 400), а не падение.
"""
import base64, binascii, io, json, logging, os, re, zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

log = logging.getLogger("tutor.chatfiles")

FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FORMATS = ("text", "docx", "pdf", "image")
MAX_FILES, MAX_FILE_BYTES = 5, 8 * 1024 * 1024
DOC_CHARS, DOCS_CHARS = 8000, 20000          # на файл / на все файлы вместе
TEXT_EXT = (".txt", ".md", ".csv")
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# ---------- вложения ----------
def _b64(s):
    if not isinstance(s, str) or not s:
        raise ValueError
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError
    return raw


def to_jpeg_b64(raw):
    """Любая картинка → JPEG ≤1600 px в base64 (kimi.build_payload помечает все изображения как jpeg)."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((1600, 1600))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
    except Exception:
        raise ValueError
    return base64.b64encode(out.getvalue()).decode()


def pdf_text(raw):
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(raw))
        return "\n".join((p.extract_text() or "") for p in r.pages[:40])[:DOC_CHARS] or "[в PDF не нашёлся текст]"
    except ImportError:
        return "[для чтения PDF на сервере нужно добавить pypdf в requirements.txt]"
    except Exception as e:
        log.warning("chat pdf: не прочитался: %r", e)
        return "[не получилось прочитать PDF]"


def docx_text(raw):
    """Текст из .docx без python-docx: абзацы word/document.xml."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > 20 * 1024 * 1024:               # защита от «бомбы»
                return "[документ слишком большой, не читается]"
            root = ET.fromstring(z.read(info))
        paras = []
        for p in root.iter(W_NS + "p"):
            s = "".join(("\t" if t.tag == W_NS + "tab" else "\n" if t.tag == W_NS + "br" else (t.text or ""))
                        for t in p.iter() if t.tag in (W_NS + "t", W_NS + "tab", W_NS + "br"))
            paras.append(s.strip())
        return "\n".join(x for x in paras if x)[:DOC_CHARS] or "[в документе не нашёлся текст]"
    except Exception as e:
        log.warning("chat docx: не прочитался: %r", e)
        return "[не получилось прочитать Word-файл]"


def read_files(raw):
    """files из запроса → (base64-картинки для vision, текст документов, имена).
    Формат: [{name, mime, data(base64)}]; None/[] — вложений нет."""
    if raw is None:
        return [], "", []
    if not isinstance(raw, list) or len(raw) > MAX_FILES:
        raise ValueError
    images, docs, names = [], [], []
    for f in raw:
        if not isinstance(f, dict):
            raise ValueError
        name, mime = f.get("name"), f.get("mime", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 160 or not isinstance(mime, str) or len(mime) > 100:
            raise ValueError
        name = re.sub(r"[\r\n\t]+", " ", name).strip()
        data = _b64(f.get("data"))
        names.append(name)
        low = name.lower()
        if mime.startswith("image/"):
            images.append(to_jpeg_b64(data))
        elif low.endswith(".pdf"):
            docs.append(f"--- {name} ---\n" + pdf_text(data))
        elif low.endswith(".docx"):
            docs.append(f"--- {name} ---\n" + docx_text(data))
        elif low.endswith(TEXT_EXT) or mime.startswith("text/"):
            docs.append(f"--- {name} ---\n" + data.decode("utf-8", "replace")[:DOC_CHARS])
        else:
            docs.append(f"--- {name} ---\n[файл такого типа пока не читается]")
    return images, "\n".join(docs)[:DOCS_CHARS], names


# ---------- разбор ответа модели в документ ----------
def _clean(s):
    return re.sub(r"\s+", " ", re.sub(r"[*_`]{1,3}", "", s)).strip()


def parse_doc(text):
    """Текст ответа → (заголовок, блоки). Блок: ("h", текст) | ("p", текст) | ("li", маркер, текст).
    Первая строка — заголовок; «# …» — раздел; «- …», «• …», «1. …» — пункты списка."""
    lines = [l.strip() for l in str(text).replace("\r", "").split("\n")]
    lines = [l for l in lines if l]
    if not lines:
        raise ValueError
    first = lines[0]
    if re.match(r"^([-*•]|\d+[.)])\s", first):            # ответ начался сразу со списка — заголовка нет
        title, body = "Ответ", lines
    else:
        title, body = _clean(first.lstrip("# ").strip()) or "Ответ", lines[1:]
    blocks = []
    for l in body:
        m = re.match(r"^(#{1,4})\s+(.*)$", l)
        if m:
            blocks.append(("h", _clean(m.group(2))))
            continue
        m = re.match(r"^(?:[-*•]|(\d+)[.)])\s+(.*)$", l)
        if m:
            blocks.append(("li", (m.group(1) + ".") if m.group(1) else "•", _clean(m.group(2))))
            continue
        blocks.append(("p", _clean(l)))
    return title[:120], [b for b in blocks if b[-1]]


def file_stem(title):
    s = re.sub(r"[^\w\- ]+", "", title, flags=re.U).strip().replace(" ", "_")
    return (s or "Ответ")[:60]


# ---------- Word ----------
def _wp(text, size=22, bold=False, ind=None, after=120, color=None):
    rpr = (f"<w:b/>" if bold else "") + f'<w:sz w:val="{size}"/>' + (f'<w:color w:val="{color}"/>' if color else "")
    ppr = f'<w:spacing w:after="{after}"/>' + (f'<w:ind w:left="{ind[0]}" w:hanging="{ind[1]}"/>' if ind else "")
    return f'<w:p><w:pPr>{ppr}</w:pPr><w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def docx_bytes(title, blocks):
    """Минимальный валидный .docx (открывается в Word, LibreOffice, Google Docs) без сторонних библиотек."""
    body = [_wp(title, 36, True, after=200, color="0F2E6E")]
    for b in blocks:
        if b[0] == "h":
            body.append(_wp(b[1], 28, True, after=100, color="0F2E6E"))
        elif b[0] == "li":
            body.append(_wp(b[1] + "\t" + b[2], 22, ind=(500, 340), after=60))
        else:
            body.append(_wp(b[1]))
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join(body) + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
           '</w:body></w:document>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return out.getvalue()


# ---------- PDF ----------
_pdf_fonts = []


def _register_pdf_fonts():
    if _pdf_fonts:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    pdfmetrics.registerFont(TTFont("ChatSans", os.path.join(FONTS, "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("ChatSans-Bold", os.path.join(FONTS, "DejaVuSans-Bold.ttf")))
    _pdf_fonts.append(1)


def pdf_bytes(title, blocks):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    _register_pdf_fonts()
    navy = HexColor("#0F2E6E")
    st = {
        "t": ParagraphStyle("t", fontName="ChatSans-Bold", fontSize=18, leading=23, textColor=navy, spaceAfter=10),
        "h": ParagraphStyle("h", fontName="ChatSans-Bold", fontSize=13.5, leading=18, textColor=navy, spaceBefore=8, spaceAfter=4),
        "p": ParagraphStyle("p", fontName="ChatSans", fontSize=11, leading=15.5, spaceAfter=5),
        "li": ParagraphStyle("li", fontName="ChatSans", fontSize=11, leading=15.5, leftIndent=16, firstLineIndent=-16, spaceAfter=3),
    }
    story = [Paragraph(escape(title), st["t"])]
    for b in blocks:
        if b[0] == "li":
            story.append(Paragraph(escape(b[1]) + "&nbsp;&nbsp;" + escape(b[2]), st["li"]))
        else:
            story.append(Paragraph(escape(b[1]), st[b[0]]))
    story.append(Spacer(1, 4))
    out = io.BytesIO()
    SimpleDocTemplate(out, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
                      title=title).build(story)
    return out.getvalue()


# ---------- картинка / инфографика: HTML модели → PDF → PNG ----------
_HTML_FONT_CSS = """@font-face { font-family: DV; src: url('%(bold)s'); font-weight: bold; }
@font-face { font-family: DV; src: url('%(reg)s'); }
html, body, div, p, span, h1, h2, h3, h4, table, tr, td, th, li, ul, ol, b, i, strong, em, small { font-family: DV; }"""


def _html_title(html):
    """Заголовок инфографики из HTML: <title> или первый h1/h2. «Инфографика» — если ничего нет."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S) or re.search(r"<h[12][^>]*>(.*?)</h[12]>", html, re.I | re.S)
    t = re.sub(r"<[^>]+>", " ", m.group(1)) if m else ""
    return _clean(t)[:90] or "Инфографика"


def image_title(html):
    """Заголовок инфографики — публичное имя для bot.py (шаг «Вишенка рисует инфографику»)."""
    return _html_title(html)


def _html_for_pdf(html):
    """HTML модели готовится к рендеру: убираем font-family (модель могла прописать шрифт без
    кириллицы) и вшиваем @font-face с DejaVu из fonts/ — иначе русский текст выйдет пустыми рамками."""
    html = re.sub(r"font-family\s*:[^;\"'}]+;?", "", html, flags=re.I)
    css = _HTML_FONT_CSS % {"reg": os.path.join(FONTS, "DejaVuSans.ttf"), "bold": os.path.join(FONTS, "DejaVuSans-Bold.ttf")}
    if "</head" in html.lower():
        i = html.lower().rfind("</head")
        return html[:i] + "<style>" + css + "</style>" + html[i:]
    return "<style>" + css + "</style>" + html


def html_pdf(html):
    """HTML-инфографика модели → PDF (xhtml2pdf) — первый шаг конвейера картинки.
    ValueError — не строка, не HTML или PDF не собрался."""
    if not isinstance(html, str) or "<html" not in html.lower():
        raise ValueError
    try:
        from xhtml2pdf import pisa
        pdf = io.BytesIO()
        st = pisa.CreatePDF(io.StringIO(_html_for_pdf(html)), dest=pdf)
        if st.err or not pdf.getvalue():
            raise ValueError
        return pdf.getvalue()
    except ValueError:
        raise
    except Exception as e:
        log.warning("html_pdf: не собрался: %r", e)
        raise ValueError


def pdf_png(pdf):
    """Страницы PDF → одна PNG-картинка 1080 px по ширине (pypdfium2) — второй шаг конвейера.
    Страницы склеиваются вертикально — высокая инфографика не обрезается.
    ValueError — PDF не отрендерился или вышел без единого тёмного пикселя (пустая картинка)."""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(pdf)
        imgs = []
        for page in doc:
            w, _h = page.get_size()
            if not w:
                continue
            imgs.append(page.render(scale=1080 / w).to_pil().convert("RGB"))
    except Exception as e:
        log.warning("pdf_png: не отрендерилось: %r", e)
        raise ValueError
    if not imgs:
        raise ValueError
    if len(imgs) == 1:
        im = imgs[0]
    else:
        from PIL import Image
        im = Image.new("RGB", (1080, sum(i.height for i in imgs)), "white")
        y = 0
        for i in imgs:
            im.paste(i, (0, y))
            y += i.height
    if im.convert("L").getextrema()[0] > 240:   # нет тёмных пикселей — текст не отрисовался
        raise ValueError
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


def html_png(html):
    """HTML-инфографика модели → PNG 1080 px по ширине за один вызов (два шага: html_pdf → pdf_png)."""
    return pdf_png(html_pdf(html))


# ---------- общий вход для api_chat ----------
def build(fmt, res):
    """(формат, ответ модели) → (байты, имя файла, заголовок). ValueError — ответ модели не годится для файла.
    res: str — для docx/pdf оформленный текст, для image — HTML-инфографика."""
    if fmt == "image":
        title = _html_title(res) if isinstance(res, str) else ""
        return html_png(res), file_stem(title or "Инфографика") + ".png", title or "Инфографика"
    title, blocks = parse_doc(res)
    if fmt == "docx":
        return docx_bytes(title, blocks), file_stem(title) + ".docx", title
    if fmt == "pdf":
        return pdf_bytes(title, blocks), file_stem(title) + ".pdf", title
    raise ValueError


def data_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()
