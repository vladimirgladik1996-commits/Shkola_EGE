"""Регресс «сломанных Word-файлов»: docx строго по схеме OOXML (ECMA-376, wml.xsd) — без сторонних валидаторов.

История бага: файлы «Чата с ИИ» объявлялись «повреждёнными» на Samsung / мобильном Word,
хотя десктопный Word их открывал. Причина — нарушения схемы OOXML в рукотворном XML
(chatfiles.docx_bytes):
  1) порядок элементов внутри w:rPr (u/b/i/rFonts/sz/color не по схеме) — исправлено 24.10 (9094aaa9);
  2) «голые» <w:p> в <w:tr> вместо <w:tc> — исправлено 25.10 (90552c71);
  3) текстовые абзацы без красной строки (w:ind w:firstLine) — исправлено 25.10 (e0d63abf).
Строгие просмотрщики (мобильный Word на Android, Samsung) отвергают такой документ как
«файл повреждён», десктопный Word открывает — поэтому баг виден только на части устройств.

Тест держит все три требования плюс порядок дочерних элементов по схеме для тех типов,
которые собирает chatfiles (rPr/pPr/sectPr/tblPr/trPr/tcPr/tblBorders/drawing/pic)."""
import io, os, re, sys, zipfile
from xml.etree import ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import chatfiles as cf


def ok(c):
    print("  ✓", c)


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
PIC = "{http://schemas.openxmlformats.org/drawingml/2006/picture}"

# порядок дочерних элементов — как в wml.xsd / dml-*.xsd (строгие просмотрщики проверяют именно его)
ORDER = {
    W + "rPr": ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike",
                "outline", "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden",
                "color", "spacing", "w", "kern", "position", "sz", "szCs", "highlight", "u", "effect",
                "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout",
                "specVanish", "oMath"],
    W + "pPr": ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
                "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
                "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
                "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap",
                "jc", "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl", "divId",
                "cnfStyle", "rPr", "sectPr", "pPrChange"],
    W + "sectPr": ["headerReference", "footerReference", "footnotePr", "endnotePr", "type", "pgSz", "pgMar",
                   "paperSrc", "pgBorders", "lnNumType", "pgNumType", "cols", "formProt", "vAlign",
                   "noEndnote", "titlePg", "textDirection", "bidi", "rtlGutter", "docGrid", "printerSettings",
                   "sectPrChange"],
    W + "tblPr": ["tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize", "tblStyleColBandSize",
                  "tblW", "jc", "tblCellSpacing", "tblInd", "tblBorders", "shd", "tblLayout", "tblCellMar",
                  "tblLook", "tblCaption", "tblDescription"],
    W + "tblBorders": ["top", "start", "left", "bottom", "end", "right", "insideH", "insideV"],
    W + "trPr": ["cnfStyle", "divId", "gridBefore", "gridAfter", "wBefore", "wAfter", "cantSplit", "trHeight",
                 "tblHeader", "tblCellSpacing", "jc", "hidden", "ins", "del", "trPrChange"],
    W + "tcPr": ["cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar",
                 "textDirection", "tcFitText", "vAlign", "hideMark", "headers", "cellIns", "cellDel",
                 "cellMerge", "tcPrChange"],
    W + "tblGrid": ["gridCol", "tblGridChange"],
    W + "tc": ["tcPr", "p"],
    W + "tr": ["trPr", "tc"],
    W + "tbl": ["tblPr", "tblGrid", "tr"],
    W + "r": ["rPr", "ins", "del", "moveFrom", "moveTo", "t", "delText", "br", "tab", "noBreakHyphen",
              "softHyphen", "drawing", "pict", "fldChar", "instrText", "delInstrText", "commentRangeStart",
              "commentRangeEnd", "commentReference", "footnoteReference", "endnoteReference"],
    WP + "inline": ["extent", "effectExtent", "docPr", "cNvGraphicFramePr", "graphic"],
    WP + "anchor": ["simplePos", "positionH", "positionV", "extent", "effectExtent", "wrapNone", "wrapSquare",
                    "wrapTight", "wrapTopAndBottom", "wrapThrough", "docPr", "cNvGraphicFramePr", "graphic"],
    PIC + "pic": ["nvPicPr", "blipFill", "spPr"],
    A + "graphicData": ["any"],
}
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u0080-\u009f\ufdd0-\ufdef\ufffe\uffff]")


def local(tag):
    return tag.rsplit("}", 1)[-1]


def check_order(root, where):
    """Порядок дочерних элементов по схеме для всех элементов с известным типом."""
    for el in root.iter():
        kids = list(el)
        rule = ORDER.get(el.tag)
        if rule is None:
            continue
        if rule == ["any"]:
            continue
        last = -1
        for k in kids:
            name = local(k.tag)
            if name not in rule:
                raise AssertionError(f"{where}: <{local(el.tag)}> неожиданный дочерний <{name}>")
            i = rule.index(name)
            assert i >= last, f"{where}: <{local(el.tag)}>: <{name}> стоит не по схеме (порядок {rule})"
            last = i


def check_docx(data, where):
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.testzip() is None, where + ": zip повреждён"
    parts = {n: z.read(n) for n in z.namelist()}
    # 1. каждая часть — валидный XML без запрещённых символов (они ломают строгие просмотрщики)
    for name, raw in parts.items():
        if not name.endswith((".xml", ".rels")):
            continue
        s = raw.decode("utf-8")
        assert not _BAD_XML.search(s), f"{where}/{name}: запрещённый символ XML"
        root = ET.fromstring(raw)
        if name.startswith("word/"):
            check_order(root, f"{where}/{name}")
    doc = ET.fromstring(parts["word/document.xml"])
    body = doc.find(W + "body")
    kids = list(body)
    # 2. тело: только абзацы/таблицы, последним — sectPr; таблица не может быть последним блоком
    assert kids[-1].tag == W + "sectPr", where + ": тело не заканчивается <w:sectPr>"
    assert all(k.tag in (W + "p", W + "tbl", W + "sectPr") for k in kids), where + ": лишний блок в теле"
    assert kids[-2].tag != W + "tbl", where + ": после таблицы обязателен <w:p>"
    # 3. строки таблиц — строго <w:tc> (баг: «голые» <w:p> в <w:tr> роняли Samsung)
    trs = doc.findall(".//" + W + "tr")
    assert trs, where + ": нет ни одной таблицы — тест теряет смысл"
    for tr in trs:
        assert all(k.tag == W + "tc" for k in tr), where + ": в <w:tr> есть не-<w:tc> (сломает Samsung)"
        for tc in tr:
            assert tc.findall(W + "p"), where + ": ячейка без <w:p>"
    # 4. красная строка 1.25 см в текстовых абзацах (баг: абзацы шли «в край»)
    for p in body.findall(W + "p"):
        txt = "".join(t.text or "" for t in p.iter(W + "t"))
        if txt.startswith(("Абзац", "Ещё абзац")):
            ind = p.find(W + "pPr/" + W + "ind")
            assert ind is not None and ind.get(W + "firstLine") == "709", \
                f"{where}: у абзаца «{txt[:20]}…» нет красной строки (w:ind w:firstLine)"
    # 5. [Content_Types].xml покрывает все части и не объявляет несуществующих
    ct = ET.fromstring(parts["[Content_Types].xml"])
    overrides = {o.get("PartName") for o in ct if local(o.tag) == "Override"}
    have = {"/" + n for n in parts
            if n.endswith(".xml") and not n.startswith("_rels") and n != "[Content_Types].xml"}
    assert overrides == have, f"{where}: Content_Types {sorted(overrides ^ have)}"
    return doc


PNG = None


def png():
    global PNG
    if PNG is None:
        from PIL import Image
        out = io.BytesIO()
        Image.new("RGB", (240, 80), (15, 46, 110)).save(out, "PNG")
        PNG = out.getvalue()
    return PNG


SRC = ("Проверка Word\n"
       "# Раздел\n"
       "Абзац с **выделением**, _____ пропуском и `кодом`.\n"
       "Ещё абзац — длинный текст, чтобы абзацное членение было видно в файле.\n"
       "- Пункт списка\n"
       "2. Нумерованный пункт\n"
       "| А | Б |\n|---|---|\n| 1 | 2 |\n"
       "```\nмоно-схема\n```\n")

title, blocks = cf.parse_doc(SRC)
doc = check_docx(cf.docx_bytes(title, blocks + [("img", png(), "Схема")]), "docx")
ok("docx проходит проверку схемы OOXML: порядок rPr/pPr/sectPr/tblPr/trPr/tcPr, xml чистый")

assert any(p.find(W + "pPr/" + W + "ind") is not None for p in doc.find(W + "body").findall(W + "p"))
ok("красная строка 1.25 см стоит в текстовых абзацах")

doc2 = check_docx(cf.docx_bytes(title, blocks, footer_all=True), "docx-footer")
ok("вариант с фирменным подвалом (таблица + плавающая картинка) тоже по схеме")

# регресс: сами проверки ЛОВЯТ исторические баги, а не просто всегда зелёные
def mutated(data, frm, to, where):
    """Ломает word/document.xml ровно так, как его ломал старый генератор;
    падает, если ломать больше нечего — значит проверки устарели."""
    zin = zipfile.ZipFile(io.BytesIO(data))
    parts = {n: zin.read(n) for n in zin.namelist()}
    doc = parts["word/document.xml"]
    assert frm in doc, f"{where}: не нашёл, что ломать — проверки устарели"
    parts["word/document.xml"] = doc.replace(frm, to)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n, raw in parts.items():
            z.writestr(n, raw)
    return out.getvalue()


one = cf.docx_bytes(title, [("table", [[[("Ячейка", {})]]])])
broken = mutated(one, b'<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>', b"", "tc")
broken = mutated(broken, b"</w:tc>", b"", "tc2")
try:
    check_docx(broken, "bad-tr")
    raise SystemExit("проверка не поймала «голые» <w:p> в <w:tr>")
except AssertionError:
    ok("проверка ловит исторический баг «<w:p> в <w:tr>» (сломанный Samsung-файл)")

old_rpr = mutated(cf.docx_bytes(title, [("p", [("текст", {"b": 1, "u": 1})])]),
                  b'<w:b/><w:sz w:val="22"/><w:u w:val="single"/>',
                  b'<w:b/><w:u w:val="single"/><w:sz w:val="22"/>', "rPr")
try:
    check_docx(old_rpr, "bad-rpr")
    raise SystemExit("проверка не поймала нарушение порядка w:rPr")
except AssertionError:
    ok("проверка ловит исторический баг порядка элементов w:rPr")

noident = mutated(cf.docx_bytes(title, [("p", [("Абзац без отступа", {})])]),
                  b'<w:ind w:firstLine="709"/>', b"", "indent")
try:
    check_docx(noident, "bad-indent")
    raise SystemExit("проверка не поймала абзац без красной строки")
except AssertionError:
    ok("проверка ловит исторический баг «абзацы без красной строки»")
