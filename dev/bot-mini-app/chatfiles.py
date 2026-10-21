"""Файлы для «Чата с ИИ»: чтение вложений мини-аппа и сборка ответа в Word / PDF / картинку.

Зависимости: stdlib + reportlab (Pillow ставится вместе с ним) + pypdf + xhtml2pdf (HTML → PDF для
инфографики) + pypdfium2 (PDF → PNG). Ничего не знает про aiohttp/aiogram, поэтому проверяется
отдельным тестом (tests/test_chat_files.py). Любая ошибка разбора вложения — ValueError
(бот ответит 400), а не падение.
"""
import base64, binascii, html as html_lib, io, json, logging, os, re, zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

log = logging.getLogger("tutor.chatfiles")

FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")   # header.png / footer.png — фирменные шапка и подвал Word
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


# Инлайн-разметка, которая ДОХОДИТ до Word/PDF как оформление (а не срезается):
#   **жирный**  *курсив*  __подчёркнутый__  `моноширинный`  _____ — пропуск (подчёркнутое пустое поле).
# Таблицы — строки «| a | b |». Схемы/графики — блок ```html …``` (картинка) или ``` …``` (моно-схема).
_RUN_RE = re.compile(
    r"(?P<blank>_{3,})"
    r"|\*\*(?P<b>.+?)\*\*"
    r"|__(?P<u>[^_]+?)__"
    r"|\*(?P<i>[^*]+?)\*"
    r"|`(?P<m>[^`]+?)`", re.S)


def _runs(s):
    """Строка → раны [(текст, стиль)]; стиль: b/i/u/m/blank (может быть пустым {}).
    Пропуски «_____» становятся подчёркнутыми неразрывными пробелами — видимым пустым полем."""
    s = re.sub(r"\s+", " ", str(s)).strip()
    out, pos = [], 0
    for m in _RUN_RE.finditer(s):
        if m.start() > pos:
            out.append((s[pos:m.start()], {}))
        g = m.groupdict()
        if g["blank"] is not None:
            out.append(("\u00a0" * max(5, min(len(g["blank"]), 20)), {"u": 1, "blank": 1}))
        elif g["b"] is not None:
            out.append((g["b"], {"b": 1}))
        elif g["u"] is not None:
            out.append((g["u"], {"u": 1}))
        elif g["i"] is not None:
            out.append((g["i"], {"i": 1}))
        else:
            out.append((g["m"], {"m": 1}))
        pos = m.end()
    if pos < len(s):
        out.append((s[pos:], {}))
    return [(t, st) for t, st in out if t]


def _plain(runs):
    return "".join(t for t, _ in runs).replace("\u00a0", " ").strip()


def _is_table_row(l):
    s = l.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def _table_cells(l):
    return [c.strip() for c in l.strip().strip("|").split("|")]


def parse_doc(text, attach=None):
    """Текст ответа → (заголовок, блоки). Блок:
    ("p", раны) | ("h", раны) | ("li", маркер, раны) | ("code", текст) |
    ("table", [ячейки…]) | ("img", png-байты).
    Первая строка — заголовок; «# …» — раздел; «- …», «• …», «1. …» — пункты списка;
    «| … |» — таблица; ```html …``` — схема-картинка (встроится в файл); ``` …``` — моно-схема."""
    raw = str(text).replace("\r", "")
    parts, pos = [], 0
    for m in re.finditer(r"```([\w+-]*)[ \t]*\n(.*?)```", raw, re.S):
        if m.start() > pos:
            parts.append(("t", raw[pos:m.start()]))
        parts.append(("f", m.group(1).lower(), m.group(2)))
        pos = m.end()
    if pos < len(raw):
        parts.append(("t", raw[pos:]))

    blocks, title, have_title = [], "Ответ", False
    rows = None

    def flush_table():
        nonlocal rows
        if rows:
            blocks.append(("table", rows))
            rows = None

    def add_line(l):
        nonlocal title, have_title, rows
        if not l.strip():
            flush_table()
            return
        if _is_table_row(l):
            cells = _table_cells(l)
            if set("".join(cells)) <= set("-: "):            # разделитель |---|
                return
            rows = (rows or []) + [[_runs(c) for c in cells]]
            return
        flush_table()
        s = l.strip()
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if not have_title:
            if re.match(r"^([-*•]|\d+[.)])\s", s):          # ответ сразу со списка — заголовка нет
                have_title = True
                title = "Ответ"
                mm = re.match(r"^(?:[-*•]|(\d+)[.)])\s+(.*)$", s)
                blocks.append(("li", (mm.group(1) + ".") if mm.group(1) else "•", _runs(mm.group(2))))
                return
            title = _plain(_runs((m.group(2) if m else s).strip())) or "Ответ"
            have_title = True
            return
        if m:
            blocks.append(("h", _runs(m.group(2))))
            return
        m = re.match(r"^(?:[-*•]|(\d+)[.)])\s+(.*)$", s)
        if m:
            blocks.append(("li", (m.group(1) + ".") if m.group(1) else "•", _runs(m.group(2))))
            return
        blocks.append(("p", _runs(s)))

    for kind, *rest in parts:
        if kind == "t":
            for l in rest[0].split("\n"):
                add_line(l)
        else:
            flush_table()
            lang, body = rest[0], rest[1].rstrip("\n")
            if "<html" in body.lower() or "<svg" in body.lower():
                try:
                    if "<img" in body.lower() and attach:
                        body = resolve_attach(body, attach)
                    blocks.append(("img", html_png(body), _html_title(body)))
                except Exception:
                    blocks.append(("code", body))                 # схема не отрендерилась — оставляем текстом
            else:
                blocks.append(("code", body))
    flush_table()
    if not blocks and not have_title:
        raise ValueError
    return title[:120], [b for b in blocks if b[-1]]


def file_stem(title):
    s = re.sub(r"[^\w\- ]+", "", title, flags=re.U).strip().replace(" ", "_")
    return (s or "Ответ")[:60]


# ---------- Word ----------
_NC = "0F2E6E"


def _force(runs, **kw):
    return [(t, {**st, **kw}) for t, st in runs]


def _run_xml(text, st, size, color=None):
    rpr = ""
    if st.get("u") or st.get("blank"):
        rpr += '<w:u w:val="single"/>'
    if st.get("b"):
        rpr += "<w:b/>"
    if st.get("i"):
        rpr += "<w:i/>"
    if st.get("m"):
        rpr += '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>'
    rpr += f'<w:sz w:val="{size}"/>'
    if color:
        rpr += f'<w:color w:val="{color}"/>'
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _para(runs, size=22, ind=None, after=120, color=None):
    ppr = f'<w:spacing w:after="{after}"/>' + (f'<w:ind w:left="{ind[0]}" w:hanging="{ind[1]}"/>' if ind else "")
    return f'<w:p><w:pPr>{ppr}</w:pPr>' + "".join(_run_xml(t, st, size, color) for t, st in runs) + "</w:p>"


def _docx_table(rows):
    ncol = max((len(r) for r in rows), default=1)
    grid = "".join('<w:gridCol w:w="2200"/>' for _ in range(ncol))
    b = "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="B9C2D4"/>'
                for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
    trs = []
    for r in rows:
        tcs = [_para(c or [("", {})], size=20, after=40) for c in r]
        tcs += ['<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr><w:p/></w:tc>'] * (ncol - len(r))
        trs.append("<w:tr>" + "".join(tcs) + "</w:tr>")
    return (f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>{b}</w:tblBorders></w:tblPr>'
            f'<w:tblGrid>{grid}</w:tblGrid>' + "".join(trs) + "</w:tbl><w:p/>")


def _docx_img(rid, docpr_id, cx, cy):
    return (f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
            f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{docpr_id}" name="Схема{docpr_id}"/>'
            f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{docpr_id}" name="Схема{docpr_id}"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
            f'</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>')


_WNS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_RNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _asset(name):
    """Байты PNG из assets/ или None (нет файла — документ собирается без фирменного оформления)."""
    try:
        with open(os.path.join(ASSETS, name), "rb") as f:
            return f.read()
    except OSError:
        return None


def _png_size(raw):
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


_DNS = ('xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')


def _pic(rid, name, cx, cy, anchor_y=None, did=1):
    """Картинка: inline (шапка) или floating позади текста на странице (anchor_y — отступ сверху в EMU)."""
    g = ('<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
         f'<pic:nvPicPr><pic:cNvPr id="0" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>'
         f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
         f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
         '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>')
    if anchor_y is None:
        return (f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
                f'<wp:docPr id="{did}" name="{name}"/><wp:cNvGraphicFramePr/>{g}</wp:inline></w:drawing></w:r>')
    return ('<w:r><w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="1" '
            'behindDoc="1" locked="0" layoutInCell="1" allowOverlap="1"><wp:simplePos x="0" y="0"/>'
            '<wp:positionH relativeFrom="margin"><wp:posOffset>0</wp:posOffset></wp:positionH>'
            f'<wp:positionV relativeFrom="page"><wp:posOffset>{anchor_y}</wp:posOffset></wp:positionV>'
            f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>'
            f'<wp:docPr id="2" name="{name}"/><wp:cNvGraphicFramePr/>{g}</wp:anchor></w:drawing></w:r>')


def docx_bytes(title, blocks, footer_all=False):
    """Валидный .docx (Word / LibreOffice / Google Docs, мобильные просмотрщики) без сторонних библиотек.
    Раны с b/i/u/mono, пропуски-подчёркивания, таблицы и встроенные картинки-схемы.
    Кроме document.xml кладём styles/settings/шапку/подвал/docProps: без них Word рисует
    угловые метки границ текста, а просмотрщики показывают файл «как есть».
    Фирменные шапка/подвал берутся из assets/ (header.png, footer.png; footer_l/footer_r
    при footer_all) — нет картинок, документ собирается без оформления."""
    body = [_para([(title, {"b": 1})], size=36, after=200, color=_NC)]
    media, rels, img_id = [], [], 0
    for b in blocks:
        k = b[0]
        if k == "h":
            body.append(_para(_force(b[1], b=1), size=28, after=100, color=_NC))
        elif k == "li":
            body.append(_para(_runs(b[1] + "\t") + b[2], size=22, ind=(500, 340), after=60))
        elif k == "code":
            for ln in b[1].split("\n"):
                body.append(_para([(ln, {"m": 1})], size=18, after=0))
        elif k == "table":
            body.append(_docx_table(b[1]))
        elif k == "img":
            img_id += 1
            png = b[1]
            rid = f"rId{100 + img_id}"
            try:
                from PIL import Image
                w, h = Image.open(io.BytesIO(png)).size
            except Exception:
                w, h = 600, 300
            cx = min(int(w) * 9525, 6_000_000)
            cy = max(1, round(cx * h / max(w, 1)))
            rels.append(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image{img_id}.png"/>')
            media.append((f"word/media/image{img_id}.png", png))
            body.append(f'<w:p>{_docx_img(rid, img_id, cx, cy)}</w:p>')
        else:
            body.append(_para(b[1], size=22, after=60))
    # фирменные шапка (первая страница) и подвал (последняя страница); нет картинок — обычный документ
    hdr_png, ftr_png = _asset("header.png"), _asset("footer.png")
    TW, PH = 6120130, 10691630                       # ширина текста (17 см) и высота страницы A4, EMU
    hdr_h = ftr_h = 0
    if hdr_png:
        w, h = _png_size(hdr_png); hdr_h = TW * h // w
    if ftr_png:
        w, h = _png_size(ftr_png); ftr_h = TW * h // w
    ftr_l, ftr_r = (_asset("footer_l.png"), _asset("footer_r.png")) if footer_all else (None, None)
    if ftr_png and not footer_all:
        pic = _pic("rId5", "Подвал", TW, ftr_h, PH - 280800 - ftr_h)
        if body[-1].endswith("</w:p>"):
            body[-1] = body[-1][:-len("</w:p>")] + pic + "</w:p>"
        else:                       # последний блок — таблица/схема: плавающая картинка идёт отдельной строкой
            body.append('<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>' + pic + '</w:p>')
    bottom = 1304 if ftr_png else 1134               # низ блока подвала — в зоне подвала (0,8 см от края), над ним поле 2,3 см
    first = ('<w:headerReference w:type="first" r:id="rId6"/><w:footerReference w:type="first" r:id="rId4"/>'
             if hdr_png else "")
    doc = (_XML + f'<w:document xmlns:w="{_WNS}" xmlns:r="{_RNS}" {_DNS}><w:body>' + "".join(body)
           + '<w:sectPr><w:headerReference w:type="default" r:id="rId3"/><w:footerReference w:type="default" r:id="rId4"/>'
           + first +
           '<w:pgSz w:w="11906" w:h="16838"/>'
           f'<w:pgMar w:top="1134" w:right="1134" w:bottom="{bottom}" w:left="1134" w:header="567" w:footer="{454 if (footer_all and ftr_l and ftr_r) else 708}" w:gutter="0"/>'
           '<w:cols w:space="708"/>' + ('<w:titlePg/>' if hdr_png else '') + '<w:docGrid w:linePitch="360"/></w:sectPr></w:body></w:document>')
    header_first = (_XML + f'<w:hdr xmlns:w="{_WNS}" xmlns:r="{_RNS}" {_DNS}><w:p><w:pPr><w:spacing w:after="0"/></w:pPr>'
                    + (_pic("rId1", "Шапка", TW, hdr_h) if hdr_png else "") + '</w:p></w:hdr>')
    empty_p = '<w:p><w:pPr><w:pStyle w:val="Normal"/><w:spacing w:after="0"/></w:pPr></w:p>'
    header = _XML + f'<w:hdr xmlns:w="{_WNS}" xmlns:r="{_RNS}">{empty_p}</w:hdr>'
    RP = '<w:rPr><w:color w:val="808080"/><w:sz w:val="18"/></w:rPr>'
    num = ('<w:r>' + RP + '<w:fldChar w:fldCharType="begin"/></w:r><w:r>' + RP + '<w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
           '<w:r>' + RP + '<w:fldChar w:fldCharType="separate"/></w:r><w:r>' + RP + '<w:t>1</w:t></w:r><w:r>' + RP + '<w:fldChar w:fldCharType="end"/></w:r>')
    if footer_all and ftr_l and ftr_r:
        # таблица 3×1 без границ: [левая часть блока] [номер страницы по центру ячейки] [сердечко] —
        # номер всегда на линии блока (выравнивание ячейки по центру), не зависит от разрядности и не переносится
        K = 0.97
        fl, fr, fh = int(TW * 1330 // 1456 * K), int(TW * 88 // 1456 * K), int(ftr_h * K)
        L, Rw = fl // 635 + 8, fr // 635 + 8
        M = 9638 - L - Rw

        def cell(w, inner, jc="left"):
            return (f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/><w:noWrap/><w:vAlign w:val="center"/></w:tcPr>'
                    f'<w:p><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/><w:jc w:val="{jc}"/></w:pPr>{inner}</w:p></w:tc>')
        footer_body = (f'<w:tbl><w:tblPr><w:tblW w:w="9638" w:type="dxa"/><w:tblLayout w:type="fixed"/>'
                       '<w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="0" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="0" w:type="dxa"/></w:tblCellMar>'
                       f'<w:tblLook w:val="0000"/></w:tblPr><w:tblGrid><w:gridCol w:w="{L}"/><w:gridCol w:w="{M}"/><w:gridCol w:w="{Rw}"/></w:tblGrid><w:tr>'
                       + cell(L, _pic("rId1", "Подвал", fl, fh, did=3)) + cell(M, num, "center") + cell(Rw, _pic("rId2", "Подвал", fr, fh, did=4), "right")
                       + '</w:tr></w:tbl><w:p><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/><w:rPr><w:sz w:val="2"/></w:rPr></w:pPr></w:p>')
    else:
        footer_body = ('<w:p><w:pPr><w:spacing w:after="0"/>' + ('<w:ind w:right="630"/><w:jc w:val="right"/>' if ftr_png and not footer_all else '<w:jc w:val="center"/>')
                       + '</w:pPr>' + num + '</w:p>')
    footer = _XML + f'<w:ftr xmlns:w="{_WNS}" xmlns:r="{_RNS}" {_DNS}>' + footer_body + '</w:ftr>'
    styles = (_XML + f'<w:styles xmlns:w="{_WNS}"><w:docDefaults><w:rPrDefault><w:rPr>'
              '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>'
              '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="ru-RU" w:eastAsia="ru-RU" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>'
              '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="259" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
              '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont"><w:name w:val="Default Paragraph Font"/><w:uiPriority w:val="1"/><w:semiHidden/></w:style>'
              '</w:styles>')
    settings = (_XML + f'<w:settings xmlns:w="{_WNS}"><w:zoom w:percent="100"/><w:defaultTabStop w:val="708"/>'
                '<w:characterSpacingControl w:val="doNotCompress"/>'
                '<w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>'
                '</w:settings>')
    core = (_XML + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>{escape(title)}</dc:title><dc:creator>Кабинет репетитора</dc:creator></cp:coreProperties>')
    app = (_XML + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
           '<Application>Microsoft Office Word</Application><AppVersion>16.0000</AppVersion></Properties>')
    W = "application/vnd.openxmlformats-officedocument.wordprocessingml."
    ct = (_XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Default Extension="png" ContentType="image/png"/>'
          f'<Override PartName="/word/document.xml" ContentType="{W}document.main+xml"/>'
          f'<Override PartName="/word/styles.xml" ContentType="{W}styles+xml"/>'
          f'<Override PartName="/word/settings.xml" ContentType="{W}settings+xml"/>'
          f'<Override PartName="/word/header1.xml" ContentType="{W}header+xml"/>'
          f'<Override PartName="/word/footer1.xml" ContentType="{W}footer+xml"/>'
          f'<Override PartName="/word/header2.xml" ContentType="{W}header+xml"/>'
          '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
          '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
          '</Types>')
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    rels0 = (_XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             f'<Relationship Id="rId1" Type="{R}officeDocument" Target="word/document.xml"/>'
             '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
             f'<Relationship Id="rId3" Type="{R}extended-properties" Target="docProps/app.xml"/>'
             '</Relationships>')
    doc_rels = (_XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{R}styles" Target="styles.xml"/>'
                f'<Relationship Id="rId2" Type="{R}settings" Target="settings.xml"/>'
                f'<Relationship Id="rId3" Type="{R}header" Target="header1.xml"/>'
                f'<Relationship Id="rId4" Type="{R}footer" Target="footer1.xml"/>'
                + (f'<Relationship Id="rId5" Type="{R}image" Target="media/footer.png"/>' if ftr_png and not footer_all else "")
                + (f'<Relationship Id="rId6" Type="{R}header" Target="header2.xml"/>' if hdr_png else "")
                + "".join(rels) + '</Relationships>')          # картинки-схемы (rId100+) — rels из цикла выше
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels0)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/settings.xml", settings)
        z.writestr("word/header1.xml", header)
        z.writestr("word/footer1.xml", footer)
        if hdr_png:
            z.writestr("word/header2.xml", header_first)
            z.writestr("word/_rels/header2.xml.rels", _XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       f'<Relationship Id="rId1" Type="{R}image" Target="media/header.png"/></Relationships>')
            z.writestr("word/media/header.png", hdr_png)
        if ftr_png:
            z.writestr("word/media/footer.png", ftr_png)
            if footer_all and ftr_l and ftr_r:
                z.writestr("word/_rels/footer1.xml.rels", _XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                           f'<Relationship Id="rId1" Type="{R}image" Target="media/footer_l.png"/>'
                           f'<Relationship Id="rId2" Type="{R}image" Target="media/footer_r.png"/></Relationships>')
                z.writestr("word/media/footer_l.png", ftr_l)
                z.writestr("word/media/footer_r.png", ftr_r)
        for name, png in media:
            z.writestr(name, png)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
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


def _para_xml(runs):
    """Раны → инлайн-разметка reportlab (<b>/<i>/<u>/<font>). Пропуски показываются видимой
    чертой («_____»), чтобы и в PDF, и в превью для проверки было ясно, где пустое поле."""
    out = []
    for t, st in runs:
        if st.get("blank"):
            t = "_" * len(t)
        t = escape(t)
        if st.get("m"):
            t = f'<font name="Courier">{t}</font>'
        if st.get("b"):
            t = f"<b>{t}</b>"
        if st.get("i"):
            t = f"<i>{t}</i>"
        if st.get("u") or st.get("blank"):
            t = f"<u>{t}</u>"
        out.append(t)
    return "".join(out)


def pdf_bytes(title, blocks):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    _register_pdf_fonts()
    navy = HexColor("#0F2E6E")
    st = {
        "t": ParagraphStyle("t", fontName="ChatSans-Bold", fontSize=18, leading=23, textColor=navy, spaceAfter=10),
        "h": ParagraphStyle("h", fontName="ChatSans-Bold", fontSize=13.5, leading=18, textColor=navy, spaceBefore=8, spaceAfter=4),
        "p": ParagraphStyle("p", fontName="ChatSans", fontSize=11, leading=15.5, spaceAfter=5),
        "li": ParagraphStyle("li", fontName="ChatSans", fontSize=11, leading=15.5, leftIndent=16, firstLineIndent=-16, spaceAfter=3),
        "code": ParagraphStyle("code", fontName="Courier", fontSize=9, leading=12, spaceAfter=5),
        "tc": ParagraphStyle("tc", fontName="ChatSans", fontSize=9.5, leading=12),
    }
    story = [Paragraph(escape(title), st["t"])]
    for b in blocks:
        k = b[0]
        if k == "li":
            story.append(Paragraph(escape(b[1]) + "&nbsp;&nbsp;" + _para_xml(b[2]), st["li"]))
        elif k == "h":
            story.append(Paragraph(_para_xml(_force(b[1], b=1)), st["h"]))
        elif k == "code":
            story.append(Paragraph("<br/>".join(escape(l) for l in b[1].split("\n")), st["code"]))
        elif k == "table":
            data = [[Paragraph(_para_xml(c or [("", {})]), st["tc"]) for c in row] for row in b[1]]
            tbl = Table(data, hAlign="LEFT")
            tbl.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#B9C2D4")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(tbl)
        elif k == "img":
            png = b[1]
            try:
                from PIL import Image as PILImage
                iw, ih = PILImage.open(io.BytesIO(png)).size
                w = 170 * mm
                story.append(RLImage(ImageReader(io.BytesIO(png)), width=w, height=w * ih / max(iw, 1)))
            except Exception:
                pass
        elif k == "p":
            story.append(Paragraph(_para_xml(b[1]), st["p"]))
        story.append(Spacer(1, 4))
    out = io.BytesIO()
    SimpleDocTemplate(out, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
                      title=title).build(story)
    return out.getvalue()


def review_shots(fmt, data, src=None, attach=None):
    """Картинки готового файла для проверки Кими. pdf/image — страницы/картинка самого файла;
    docx — превью из ТЕХ ЖЕ блоков (Word без Office не растрируется, а блоки — те же, что в файле)."""
    if fmt != "docx":
        return render_shots(fmt, data)
    try:
        title, blocks = parse_doc(src, attach)
        return render_shots("pdf", pdf_bytes(title, blocks))
    except Exception as e:
        log.warning("review_shots: превью docx не собралось: %r", e)
        return []


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
def resolve_attach(html, attach):
    """Картинки в HTML модели: <img src="attach:имя_файла"> → data-URI из вложений пользователя.
    Всё, что не встроилось (чужой URL, неизвестное имя), убирается — в готовом файле не должно
    остаться «битых» картинок."""
    def rep(m):
        tag = m.group(0)
        s = re.search(r'''src\s*=\s*["']([^"']*)["']''', tag, re.I)
        if not s:
            return ""
        url = s.group(1).strip()
        if url.lower().startswith("data:image/"):
            return tag
        if url.lower().startswith("attach:"):
            got = (attach or {}).get(url[7:].strip())
            return (tag[:s.start()] + 'src="%s"' % got + tag[s.end():]) if got else ""
        return ""
    return re.sub(r"<img\b[^>]*>", rep, html, flags=re.I)


def _is_html_doc(s):
    t = str(s).lstrip()[:300].lower()
    return t.startswith("<html") or t.startswith("<!doctype")


def build(fmt, res, attach=None):
    """(формат, ответ модели) → (байты, имя файла, заголовок). ValueError — ответ модели не годится для файла.
    res: str — для docx — оформленный текст (с ```html```-схемами и таблицами), для pdf — HTML-документ
    (или текст: соберём из него), для image — HTML-инфографика. attach — картинки из вложений
    пользователя (имя → data-URI) для <img src=\"attach:имя\"> в HTML/схемах."""
    if not isinstance(res, str) or not res.strip():
        raise ValueError                        # не строка или пустой ответ — файла не будет
    if fmt == "image":
        if "<img" in res.lower():
            res = resolve_attach(res, attach)
        title = _html_title(res)
        return html_png(res), file_stem(title or "Инфографика") + ".png", title or "Инфографика"
    if fmt == "pdf" and _is_html_doc(res):
        if "<img" in res.lower():
            res = resolve_attach(res, attach)
        try:                                    # основной путь: модель рисует HTML, мы конвертируем в PDF
            title = _html_title(res) or "Документ"
            return html_pdf(res), file_stem(title) + ".pdf", title
        except ValueError:
            log.warning("build: HTML не собрался в PDF — собираю PDF из текста")
            res = _html_text(res)
    if _is_html_doc(res):
        res = _html_text(res)                  # модель прислала HTML там, где ждали текст — вытаскиваем текст
    title, blocks = parse_doc(res, attach)
    if fmt == "docx":
        return docx_bytes(title, blocks), file_stem(title) + ".docx", title
    if fmt == "pdf":
        return pdf_bytes(title, blocks), file_stem(title) + ".pdf", title
    raise ValueError


def data_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


# ---------- подстановка картинок вложений и проверка готового файла моделью ----------
def _html_text(src):
    """Грубый сброс тегов: модель прислала HTML там, где ждали текст (или PDF из HTML не собрался) —
    остаётся читаемый текст с разбивкой на строки для parse_doc."""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", str(src), flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|h[1-6]|li|tr|table|section)>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "\n- ", s, flags=re.I)
    s = re.sub(r"<h([1-6])[^>]*>", "\n# ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_lib.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    return "\n".join(l.strip() for l in s.split("\n") if l.strip())


def _jpeg_b64(im, maxw=1200):
    """Страница/картинка → JPEG в base64 (такой формат ждёт kimi.build_payload для vision)."""
    from PIL import Image
    im = im.convert("RGB")
    if im.width > maxw:
        im = im.resize((maxw, max(1, round(im.height * maxw / im.width))), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return base64.b64encode(out.getvalue()).decode()


def render_shots(fmt, data, max_pages=3):
    """Готовый файл → картинки страниц для проверки моделью (JPEG в base64, не больше max_pages).
    Не отрендерилось — просто нет картинок: проверка пропустится, файл не сломается."""
    try:
        if fmt == "image":
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            im.load()
            return [_jpeg_b64(im)]
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(data)
        out = []
        for k in range(min(len(doc), max_pages)):
            page = doc[k]
            w, _h = page.get_size()
            if not w:
                continue
            out.append(_jpeg_b64(page.render(scale=min(2.0, 1200 / w)).to_pil()))
        return out
    except Exception as e:
        log.warning("render_shots: не отрендерились страницы для проверки: %r", e)
        return []
