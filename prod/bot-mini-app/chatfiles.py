"""Файлы для «Чата с ИИ»: чтение вложений мини-аппа и сборка ответа в Word / PDF / картинку.

Только stdlib + reportlab (Pillow ставится вместе с ним) + pypdf — новых зависимостей нет.
Ничего не знает про aiohttp/aiogram, поэтому проверяется отдельным тестом (tests/test_chat_files.py).
Любая ошибка разбора вложения — ValueError (бот ответит 400), а не падение.
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


# ---------- картинка / инфографика ----------
def parse_infographic(obj):
    """JSON модели → (заголовок, подзаголовок, [(заголовок пункта, текст)]). ValueError, если пунктов нет."""
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        raise ValueError
    items = []
    for it in obj["items"][:8]:
        if isinstance(it, dict):
            h, t = _clean(str(it.get("h", ""))), _clean(str(it.get("t", "")))
            if h or t:
                items.append((h[:70], t[:240]))
    if not items:
        raise ValueError
    title = _clean(str(obj.get("title") or "Инфографика"))[:90] or "Инфографика"
    return title, _clean(str(obj.get("subtitle") or ""))[:140], items


def infographic_png(title, subtitle, items):
    """Карточка в цветах приложения: кремовый фон, тёмно-синий текст, жёлтый акцент."""
    from PIL import Image, ImageDraw, ImageFont
    W, PAD = 1080, 56
    f = lambda n, s: ImageFont.truetype(os.path.join(FONTS, n), s)
    ft, fs, fh, fb, fn = f("DejaVuSans-Bold.ttf", 52), f("DejaVuSans.ttf", 28), f("DejaVuSans-Bold.ttf", 34), f("DejaVuSans.ttf", 30), f("DejaVuSans-Bold.ttf", 30)
    BG, TX, HINT, CARD, YEL, NAVY, LN = "#FBF7E9", "#0E2250", "#66719A", "#FFFFFF", "#FFC93C", "#0F2E6E", "#E9E1C8"
    probe = ImageDraw.Draw(Image.new("RGB", (W, 10)))

    def wrap(text, font, width):
        lines, cur = [], ""
        for word in text.split():
            t = (cur + " " + word).strip()
            if probe.textlength(t, font=font) <= width or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines or [""]

    tl = wrap(title, ft, W - 2 * PAD)
    sl = wrap(subtitle, fs, W - 2 * PAD) if subtitle else []
    inner = W - 2 * PAD - 2 * 28 - 76                       # ширина текста внутри карточки
    cards = []
    for h, t in items:
        hl, bl = (wrap(h, fh, inner) if h else []), (wrap(t, fb, inner) if t else [])
        cards.append((hl, bl, 28 * 2 + len(hl) * 44 + len(bl) * 40 + (8 if hl and bl else 0)))
    H = PAD + len(tl) * 64 + len(sl) * 38 + 36 + sum(c[2] + 20 for c in cards) + 70
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    y = PAD
    d.rounded_rectangle((PAD, y - 18, PAD + 90, y - 10), 4, fill=YEL)
    for l in tl:
        d.text((PAD, y), l, font=ft, fill=TX)
        y += 64
    for l in sl:
        d.text((PAD, y + 4), l, font=fs, fill=HINT)
        y += 38
    y += 36
    for i, (hl, bl, ch) in enumerate(cards, 1):
        d.rounded_rectangle((PAD, y, W - PAD, y + ch), 26, fill=CARD, outline=LN, width=2)
        cx, cy = PAD + 28 + 24, y + 28 + 24
        d.ellipse((cx - 24, cy - 24, cx + 24, cy + 24), fill=YEL)
        d.text((cx, cy), str(i), font=fn, fill=TX, anchor="mm")
        ty = y + 28
        for l in hl:
            d.text((PAD + 28 + 76, ty), l, font=fh, fill=NAVY)
            ty += 44
        if hl and bl:
            ty += 8
        for l in bl:
            d.text((PAD + 28 + 76, ty), l, font=fb, fill=TX)
            ty += 40
        y += ch + 20
    d.text((W // 2, H - 36), "Надежда · Кабинет репетитора", font=fs, fill=HINT, anchor="mm")
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


# ---------- общий вход для api_chat ----------
def build(fmt, res):
    """(формат, ответ модели) → (байты, имя файла, заголовок). ValueError — ответ модели не годится для файла.
    res: str для docx/pdf, dict (JSON инфографики) для image."""
    if fmt == "image":
        title, sub, items = parse_infographic(res)
        return infographic_png(title, sub, items), file_stem(title) + ".png", title
    title, blocks = parse_doc(res)
    if fmt == "docx":
        return docx_bytes(title, blocks), file_stem(title) + ".docx", title
    if fmt == "pdf":
        return pdf_bytes(title, blocks), file_stem(title) + ".pdf", title
    raise ValueError


def data_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()
