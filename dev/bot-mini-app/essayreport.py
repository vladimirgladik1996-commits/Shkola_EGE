"""«ИИ-проверка»: отчёт о разборе сочинения в PDF (A4) и Word — для отправки в Telegram.

Сборка — тем же кодом, что и файлы «Чата с ИИ» (chatfiles): Word — chatfiles.docx_bytes, PDF — reportlab
с теми же шрифтами DejaVu (chatfiles._register_pdf_fonts). Вёрстка страниц — по макету
mockups/ai-check-a4-preview.html: стр. 1 — шапка, балл, статистика, текст работы с пометками;
стр. 2 — таблица ошибок; стр. 3 — критерии ФИПИ и рекомендации. Данные — нормализованный разбор
essaycheck._result(); пустые блоки (missing) показываются честной пометкой, ничего не додумывается.
Зависимости: stdlib + reportlab (как у chatfiles). Ничего не знает про aiohttp/aiogram — проверяется
отдельным тестом (tests/test_essay_report.py).
"""
import io, logging, os, re
from datetime import datetime
from xml.sax.saxutils import escape

import chatfiles

log = logging.getLogger("tutor.essayreport")

HERE = os.path.dirname(os.path.abspath(__file__))
LOGO = os.path.join(HERE, "assets", "essay-logo.jpg")      # аватар Вишенки из макета A4
BRAND = "Школа ЕГЭ · ИИ-проверка"
WORK = {"ege": ("Разбор сочинения ЕГЭ", "Русский язык · сочинение по прочитанному тексту"),
        "oge": ("Разбор эссе ОГЭ", "Русский язык · эссе для ОГЭ")}
TYPES = ("Орфография", "Пунктуация", "Речь")
MISSING = {"text": "Текст работы не распознан.", "errors": "Ошибки не найдены или не подтверждены текстом.",
           "criteria": "Разбор по критериям ФИПИ получить не удалось.",
           "recommendations": "Рекомендации не сформированы."}
NOTE = ("Разбор выполнен ИИ по критериям ФИПИ и носит учебный характер: итоговый балл на экзамене выставляет "
        "эксперт. Текст распознан с фотографий рукописи, ошибки ученика сохранены без исправлений.")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")


# ---------- общие данные ----------
def _kind(kind):
    return kind if kind in WORK else "ege"


def _date(now):
    return f"{now.day} {MONTHS[now.month - 1]} {now.year}"


def _plural(n):
    a, b = abs(n) % 100, abs(n) % 10
    return "баллов" if 10 < a < 20 else "балла" if 1 < b < 5 else "балл" if b == 1 else "баллов"


def potential(r):
    """«потенциал +N баллов»: сумма эффектов рекомендаций («+1 К2» → 1) — как ecPot() во фронте."""
    s = 0
    for x in r.get("recommendations") or []:
        m = re.search(r"\+\s*(\d+)", x.get("badge") or "")
        s += int(m.group(1)) if m else 1
    return s


def _counts(r):
    c = {t: 0 for t in TYPES}
    for e in r.get("errors") or []:
        c[e["type"]] = c.get(e["type"], 0) + 1
    return c


def _crit_state(c):
    """(класс карточки, подпись): w — частично, r — основная потеря (≥ половины баллов потеряно)."""
    got, mx = c["got"], c["max"]
    if got >= mx:
        return "", "полный балл"
    if got * 2 <= mx and mx > 1:
        return "r", "основная потеря"
    return "w", "частично"


def mark_spans(text, errors):
    """Ошибки → непересекающиеся отрезки текста [(начало, конец, номер)]: поиск фрагмента `found`
    без учёта регистра, ё=е и знаков препинания между словами («считаю, что» найдёт «считаю что»).
    Фрагмент, которого нет в тексте, просто не помечается (в таблице ошибок он остаётся)."""
    spans = []
    for e in errors or []:
        words = re.findall(r"\w+", str(e.get("found") or ""), re.U)
        if not words:
            continue
        pat = r"(?<!\w)" + r"[^\w\n]+".join(re.escape(w).replace("ё", "[её]").replace("е", "[её]")
                                             for w in words) + r"(?!\w)"
        for m in re.finditer(pat, text, re.I):
            if all(m.end() <= a or m.start() >= b for a, b, _ in spans):
                spans.append((m.start(), m.end(), e["i"]))
                break
    return sorted(spans)


def _paras(text, errors):
    """Текст работы → абзацы, каждый — список кусков (строка, номер ошибки | None)."""
    text = (text or "").replace("\r", "")
    spans, out, pos = mark_spans(text, errors), [], 0
    pieces = []
    for a, b, i in spans:
        pieces.append((text[pos:a], None))
        pieces.append((text[a:b], i))
        pos = b
    pieces.append((text[pos:], None))
    cur = []
    for s, i in pieces:
        parts = re.split(r"\n+", s)
        for k, part in enumerate(parts):
            if k:                                  # перевод строки — новый абзац
                out.append(cur)
                cur = []
            if part:
                cur.append((part, i))
    out.append(cur)
    return [p for p in out if "".join(s for s, _ in p).strip()]


def _title(kind):
    return WORK[_kind(kind)][0]


def file_name(kind, student="", now=None):
    now = now or datetime.now()
    who = chatfiles.file_stem(student) if student else ""
    return chatfiles.file_stem(f"{_title(kind)} {who}".strip()) + f"_{now:%Y-%m-%d}"


def _number(now):
    return f"{now:%d%m}-{now:%H%M}"


# ---------- Word (chatfiles.docx_bytes) ----------
def docx_report(r, kind="ege", student="", now=None):
    """Word-отчёт тем же построителем, что и файлы «Чата с ИИ»: заголовок, абзацы, таблицы, списки."""
    now = now or datetime.now()
    sc, miss = r.get("score") or {}, r.get("missing") or []
    B, U = {"b": 1}, {"u": 1, "b": 1}
    P = lambda t, st=None: ("p", [(t, st or {})])
    blocks = [P(f"{WORK[_kind(kind)][1]} · {_date(now)} · Отчёт № {_number(now)}", {"i": 1})]
    if student:
        blocks.append(P(f"Ученик: {student}", B))
    blocks.append(("h", [("Прогнозный балл", {})]))
    if "criteria" in miss:
        blocks.append(P(MISSING["criteria"]))
    else:
        blocks.append(P(f"{sc.get('got', 0)} из {sc.get('max', 0)} · {sc.get('level', '')} · {sc.get('percent', 0)}%", B))
        pot = potential(r)
        if pot:
            blocks.append(P(f"Потенциал: +{pot} {_plural(pot)}"))
    if "errors" not in miss:
        c = _counts(r)
        blocks.append(("h", [("Статистика ошибок", {})]))
        blocks.append(P(" · ".join(f"{t} — {c[t]}" for t in TYPES)))
    blocks.append(("h", [("Текст работы с пометками", {})]))
    if "text" in miss:
        blocks.append(P(MISSING["text"]))
    else:
        for para in _paras(r.get("text"), r.get("errors")):
            runs = []
            for s, i in para:
                runs.append((s, U if i else {}))
                if i:
                    runs.append((f"[{i}]", B))
            blocks.append(("p", runs))
        blocks.append(P("Подчёркнуто жирным — найденные ошибки, [N] — номер в списке разбора. "
                        "Текст распознан с фото, ошибки ученика сохранены.", {"i": 1}))
    blocks.append(("h", [("Разбор ошибок", {})]))
    errs = r.get("errors") or []
    if not errs:
        blocks.append(P(MISSING["errors"] if "errors" in miss else "Ошибок не найдено."))
    else:
        # таблицы chatfiles._docx_table в Word пустые (ячейки без <w:tc>) — ошибки идут нумерованным списком
        for e in errs:
            blocks.append(("li", f"{e['i']}.", [(f"[{e['type']}] ", B), (e["found"], {}), (" → ", {}),
                                                (e["correct"] or "—", B), (". " + e["why"], {}),
                                                (f" ({e['src']})", {"i": 1})]))
    blocks.append(("h", [("Критерии ФИПИ с обоснованием", {})]))
    if "criteria" in miss:
        blocks.append(P(MISSING["criteria"]))
    for c in r.get("criteria") or []:
        blocks.append(P(f"{c['name']} — {c['got']} / {c['max']} · {_crit_state(c)[1]}", B))
        if c.get("why"):
            blocks.append(("p", [("Почему: ", B), (c["why"], {})]))
        if c.get("tip"):
            blocks.append(("p", [("Совет: ", B), (c["tip"], {})]))
    if r.get("recommendations"):
        blocks.append(("h", [("Рекомендации", {})]))
        for n, x in enumerate(r["recommendations"], 1):
            runs = [(x["title"], B)] + ([(". " + x["text"], {})] if x.get("text") else []) \
                   + ([("  " + x["badge"], {"i": 1})] if x.get("badge") else [])
            blocks.append(("li", f"{n}.", runs))
    blocks.append(P(NOTE, {"i": 1}))
    return chatfiles.docx_bytes(_title(kind), blocks)


# ---------- PDF A4 (reportlab, шрифты как у chatfiles) ----------
TX, HINT, LN = "#0E2250", "#3F4A6E", "#8C93AA"
_fonts_done = []


def _fonts():
    """Те же файлы DejaVu из fonts/, что у chatfiles, но под своим семейством ES: теги <b> в абзацах
    отчёта должны давать жирный DejaVu (у chatfiles-имён семейство не задано, чат на это не рассчитан)."""
    if _fonts_done:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    pdfmetrics.registerFont(TTFont("ES", os.path.join(chatfiles.FONTS, "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("ES-Bold", os.path.join(chatfiles.FONTS, "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFontFamily("ES", normal="ES", bold="ES-Bold", italic="ES", boldItalic="ES-Bold")
    _fonts_done.append(1)


def pdf_report(r, kind="ege", student="", now=None):
    """PDF A4 по макету ai-check-a4-preview.html (поля 12/14 мм, шапка и подвал на каждой странице)."""
    from reportlab.graphics.shapes import Drawing, Rect, String, Wedge
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageTemplate, Paragraph, Spacer, Table,
                                    TableStyle)
    _fonts()
    now = now or datetime.now()
    R, BD = "ES", "ES-Bold"
    tx, hint, ln = HexColor(TX), HexColor(HINT), HexColor(LN)
    sc, miss = r.get("score") or {}, r.get("missing") or []
    num, when = _number(now), _date(now)
    sub = student or "разбор подготовила Вишенка"
    W = A4[0] - 28 * mm                                   # ширина текста: поля 14 мм

    def S(name, **kw):
        base = dict(fontName=R, fontSize=9.6, leading=13.5, textColor=HexColor("#111111"))
        base.update(kw)
        return ParagraphStyle(name, **base)
    s_h1, s_sub = S("h1", fontName=BD, fontSize=19, leading=23, textColor=tx), \
        S("sub", fontName=BD, fontSize=9.5, leading=12, textColor=HexColor("#333333"), spaceAfter=8)
    s_h2 = S("h2", fontName=BD, fontSize=12, leading=15, textColor=tx, spaceBefore=11, spaceAfter=6)
    s_p, s_sm = S("p"), S("sm", fontSize=8.4, leading=11.5, textColor=HexColor("#333333"))
    s_essay = S("es", fontSize=10.2, leading=21)
    s_th = S("th", fontName=BD, fontSize=8, leading=10, textColor=HexColor("#222222"))
    s_c = S("c", fontSize=9, leading=12.5)
    s_kt = S("kt", fontName=BD, fontSize=10, leading=13, textColor=tx)

    def esc_(t):
        return escape(str(t))

    def h2(t, tag=""):
        t = Table([[Paragraph(esc_(t), s_h2), Paragraph(esc_(tag), S("tg", fontName=BD, fontSize=8.4, alignment=2,
                                                                      textColor=tx)) if tag else ""]],
                  colWidths=[W * .66, W * .34])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        return t

    def bar(frac, w, h=7):
        d = Drawing(w, h)
        w -= 2
        d.add(Rect(1, 1, w, h - 2, rx=3, ry=3, fillColor=white, strokeColor=tx, strokeWidth=1))
        f = max(0.0, min(1.0, frac))
        if f:
            d.add(Rect(1, 1, max(h - 2, w * f), h - 2, rx=3, ry=3, fillColor=tx, strokeColor=tx, strokeWidth=1))
        return d

    def gauge(got, mx, size=74):
        d, c, ro, ri = Drawing(size, size), size / 2, size / 2 - 1, size / 2 - 9
        d.add(Wedge(c, c, ro, 0, 360, radius1=ri, fillColor=HexColor("#C9CEDD"), strokeColor=None))
        p = got / mx if mx else 0
        if p > 0:
            d.add(Wedge(c, c, ro, 90 - 360 * min(p, .9999), 90, radius1=ri, fillColor=tx, strokeColor=None))
        d.add(String(c, c - 3, str(got), fontName=BD, fontSize=22, fillColor=tx, textAnchor="middle"))
        d.add(String(c, c - 16, f"из {mx}", fontName=BD, fontSize=7.5, fillColor=tx, textAnchor="middle"))
        return d

    def box(rows, widths, style, **kw):
        t = Table(rows, colWidths=widths, **kw)
        t.setStyle(TableStyle(style))
        return t
    story = [Paragraph(esc_(_title(kind)), s_h1),
             Paragraph(esc_(WORK[_kind(kind)][1]), s_sub)]

    # --- hero: балл ---
    if "criteria" in miss:
        story.append(box([[Paragraph(esc_(MISSING["criteria"]), s_p)]], [W],
                         [("BOX", (0, 0), (-1, -1), 1.2, ln), ("TOPPADDING", (0, 0), (-1, -1), 8),
                          ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    else:
        got, mx, pot = sc.get("got", 0), sc.get("max", 0), potential(r)
        c = _counts(r)
        mid = [Paragraph("ПРОГНОЗНЫЙ БАЛЛ", S("lb", fontName=BD, fontSize=8, textColor=hint)),
               Paragraph(f'<font backColor="{TX}" color="white"><b>&nbsp;{esc_(sc.get("level", ""))} · '
                         f'{sc.get("percent", 0)}%&nbsp;</b></font>', S("lv", fontName=BD, fontSize=10.5,
                                                                          leading=20)),
               Paragraph(f"<b>{esc_(student)}</b>" if student else "", s_p)]
        info = Paragraph(f"Критериев: {len(r.get('criteria') or [])}<br/>Ошибок найдено: {len(r.get('errors') or [])}"
                         + (f"<br/>Потенциал: +{pot} {_plural(pot)}" if pot else ""),
                         S("inf", fontName=BD, fontSize=8.4, leading=13, alignment=2, textColor=hint))
        tiles = box([[Paragraph(f"<b>{c[t]}</b>", S("tn", fontName=BD, fontSize=15, leading=18, alignment=1,
                                                     textColor=tx)) for t in TYPES],
                      [Paragraph(t.lower() if t != "Речь" else "речь", S("tl", fontName=BD, fontSize=8,
                                                                         alignment=1)) for t in TYPES]],
                    [(W - 24) / 3] * 3,
                    [("BOX", (0, 0), (0, -1), 1.2, tx), ("BOX", (1, 0), (1, -1), 1.2, tx), ("BOX", (2, 0), (2, -1), 1.2, tx),
                     ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]) \
            if "errors" not in miss else ""
        hero = box([[gauge(got, mx), mid, info], [tiles, "", ""]], [84, W - 84 - 135, 135],
                   [("BOX", (0, 0), (-1, -1), 2, tx), ("ROUNDEDCORNERS", [10, 10, 10, 10]),
                    ("VALIGN", (0, 0), (-1, 0), "MIDDLE"), ("SPAN", (0, 1), (-1, 1)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)])
        story.append(hero)

    # --- статистика ошибок ---
    if "errors" not in miss:
        c = _counts(r)
        mxc = max(max(c.values()), 1)
        story.append(h2("Статистика ошибок"))
        for t in TYPES:
            story.append(box([[Paragraph(f"<b>{t}</b>", S("st", fontName=BD, fontSize=9)), bar(c[t] / mxc, W - 190),
                               Paragraph(f"<b>{c[t]}</b>", S("sn", fontName=BD, fontSize=9, alignment=2,
                                                             textColor=tx))]],
                             [96, W - 190 + 6, 40],
                             [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 2),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    # --- текст работы с пометками ---
    story.append(h2("Текст работы с пометками"))
    if "text" in miss:
        story.append(Paragraph(esc_(MISSING["text"]), s_p))
    else:
        paras = []
        for para in _paras(r.get("text"), r.get("errors")):
            x = ""
            for s, i in para:
                x += (f'<font backColor="#EDEDED"><u>{esc_(s)}</u></font><super><font name="{BD}" size="6.5">{i}'
                      f'</font></super>') if i else esc_(s)
            paras.append(Paragraph(x, s_essay))
        story.append(box([[paras]], [W], [("BOX", (0, 0), (-1, -1), 1.2, ln), ("ROUNDEDCORNERS", [8, 8, 8, 8]),
                                          ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                          ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        story.append(Paragraph("Серым и подчёркнуто — найденные ошибки, цифра — номер в таблице разбора. "
                               "Текст распознан с фото, ошибки ученика сохранены.", s_sm))

    # --- таблица ошибок (стр. 2) ---
    from reportlab.platypus import PageBreak
    story += [PageBreak(), h2("Разбор ошибок", f"{len(r.get('errors') or [])} подтверждены текстом"
                             if r.get("errors") else "")]
    errs = r.get("errors") or []
    if not errs:
        story.append(Paragraph(esc_(MISSING["errors"] if "errors" in miss else "Ошибок не найдено."), s_p))
    else:
        rows = [[Paragraph(h, s_th) for h in ("№", "Фрагмент и исправление", "Правило и источник")]]
        for e in errs:
            rows.append([Paragraph(f"<b>{e['i']}</b>", S("n", fontName=BD, fontSize=9.5, alignment=1, textColor=tx)),
                         Paragraph(f"<b>{esc_(e['type'].upper())}</b><br/><strike>{esc_(e['found'])}</strike> → "
                                   f"<b>{esc_(e['correct'] or '—')}</b>", s_c),
                         Paragraph(f"{esc_(e['why'])}<br/><font size=\"7.6\" color=\"#333333\"><b>{esc_(e['src'])}"
                                   f"</b></font>", s_c)])
        t = Table(rows, colWidths=[26, W * .38, W - 26 - W * .38], repeatRows=1)
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), .8, ln),
                               ("LINEBELOW", (0, 0), (-1, 0), 1.4, tx), ("TOPPADDING", (0, 0), (-1, -1), 5),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        story.append(t)

    # --- критерии ФИПИ и рекомендации (стр. 3) ---
    crits = r.get("criteria") or []
    story += [PageBreak(), h2("Критерии ФИПИ с обоснованием", f"{sc.get('got', 0)} из {sc.get('max', 0)}"
                              if crits else "")]
    if not crits:
        story.append(Paragraph(esc_(MISSING["criteria"]), s_p))
    for c in crits:
        cls, lab = _crit_state(c)
        body = [[Paragraph(esc_(c["name"]), s_kt),
                 Paragraph(f'<b>{c["got"]} / {c["max"]} · {lab}</b>', S("kp", fontName=BD, fontSize=9, alignment=2,
                                                                       textColor=tx))],
                [bar(c["got"] / c["max"], W - 60, 6), ""]]
        if c.get("why"):
            body.append([Paragraph(f"<b>Почему:</b> {esc_(c['why'])}", s_c), ""])
        if c.get("tip"):
            body.append([Paragraph(f"<b>Совет:</b> {esc_(c['tip'])}", s_c), ""])
        t = Table(body, colWidths=[W - 170, 140])
        t.setStyle(TableStyle([("SPAN", (0, i), (1, i)) for i in range(1, len(body))] +
                              [("BOX", (0, 0), (-1, -1), 1, ln), ("ROUNDEDCORNERS", [6, 6, 6, 6]),
                               ("LINEBEFORE", (0, 0), (0, -1), {"": 4, "w": 6, "r": 9}[cls], tx),
                               ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 1),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        story += [KeepTogether([t]), Spacer(1, 3)]
    recs = r.get("recommendations") or []
    if recs:
        pot = potential(r)
        story.append(h2("Рекомендации", f"потенциал +{pot} {_plural(pot)}" if pot else ""))
        rows = [[Paragraph(f"<b>{n}</b>", S("rn", fontName=BD, fontSize=10, alignment=1, textColor=tx)),
                 [Paragraph(f"<b>{esc_(x['title'])}</b>", S("rt", fontName=BD, fontSize=10, leading=13)),
                  Paragraph(esc_(x.get("text", "")), s_c)] + ([Paragraph(f"<b>{esc_(x['badge'])}</b>", s_sm)]
                                                               if x.get("badge") else [])]
                for n, x in enumerate(recs, 1)]
        t = Table(rows, colWidths=[26, W - 26])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -2), .6, ln),
                               ("BOX", (0, 0), (-1, -1), 1, ln), ("ROUNDEDCORNERS", [8, 8, 8, 8]),
                               ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        story.append(t)
    story += [Spacer(1, 6), box([[Paragraph(esc_(NOTE), s_sm)]], [W],
                                [("BOX", (0, 0), (-1, -1), 1, ln), ("ROUNDEDCORNERS", [6, 6, 6, 6])])]

    # --- шапка и подвал каждой страницы (как .top и .foot макета) ---
    class Numbered(rl_canvas.Canvas):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            n = len(self._pages)
            for st in self._pages:
                self.__dict__.update(st)
                self._chrome(n)
                super().showPage()
            super().save()

        def _chrome(self, n):
            Wp, Hp = A4
            top = Hp - 12 * mm
            if os.path.exists(LOGO):
                try:
                    self.drawImage(LOGO, 14 * mm, top - 9 * mm, 9 * mm, 9 * mm, mask="auto")
                except Exception:
                    pass
            self.setFillColor(tx); self.setFont(BD, 10.5); self.drawString(26 * mm, top - 4 * mm, BRAND)
            self.setFillColor(HexColor("#333333")); self.setFont(BD, 7.6)
            self.drawString(26 * mm, top - 8 * mm, sub[:70])
            self.setFont(BD, 7.8); self.setFillColor(HexColor("#222222"))
            self.drawRightString(Wp - 14 * mm, top - 3.4 * mm, when)
            self.drawRightString(Wp - 14 * mm, top - 7.4 * mm, f"Отчёт № {num}")
            self.setStrokeColor(tx); self.setLineWidth(1.6); self.line(14 * mm, top - 11 * mm, Wp - 14 * mm, top - 11 * mm)
            self.setLineWidth(.9); self.setStrokeColor(HexColor("#222222")); self.line(14 * mm, 12 * mm, Wp - 14 * mm, 12 * mm)
            self.setFont(BD, 7.6)
            self.drawString(14 * mm, 8 * mm, BRAND)
            self.drawRightString(Wp - 14 * mm, 8 * mm, f"Стр. {self._pageNumber} из {n}")

    out = io.BytesIO()
    doc = BaseDocTemplate(out, pagesize=A4, title=_title(kind), author=BRAND,
                          leftMargin=14 * mm, rightMargin=14 * mm, topMargin=27 * mm, bottomMargin=16 * mm)
    doc.addPageTemplates([PageTemplate(id="a4", frames=[Frame(14 * mm, 16 * mm, W, A4[1] - 43 * mm, id="f",
                                                              leftPadding=0, rightPadding=0, topPadding=0,
                                                              bottomPadding=0)])])
    doc.build(story, canvasmaker=Numbered)
    return out.getvalue()


def build(r, kind="ege", student="", now=None):
    """Разбор → {"pdf": (байты, имя), "docx": (байты, имя)}. Формат, который не собрался, пропускается
    (в лог — причина): отчёт не должен ронять проверку, а один формат — лишать второго."""
    now = now or datetime.now()
    stem, out = file_name(kind, student, now), {}
    for ext, fn in (("pdf", pdf_report), ("docx", docx_report)):
        try:
            out[ext] = (fn(r, kind, student, now), f"{stem}.{ext}")
        except Exception as e:
            log.exception("essayreport: %s не собрался: %r", ext, e)
    return out


def caption(r, kind="ege", student=""):
    """Подпись к файлам в Telegram: тип работы, ученик, итоговый балл."""
    sc = r.get("score") or {}
    head = _title(kind) + (f" · {student}" if student else "")
    if "criteria" in (r.get("missing") or []):
        return head
    return f"{head}\nПрогнозный балл: {sc.get('got', 0)} из {sc.get('max', 0)} · {sc.get('level', '')}"
