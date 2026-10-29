"""«ИИ-проверка»: отчёт о разборе сочинения в PDF (A4) и Word — для отправки в Telegram.

Сборка — тем же кодом, что и файлы «Чата с ИИ» (chatfiles): Word — chatfiles.docx_bytes, PDF — reportlab
с теми же шрифтами DejaVu (chatfiles._register_pdf_fonts). Вёрстка страниц — по макету
mockups/ai-check-a4-preview.html: стр. 1 — шапка, балл, статистика, текст работы с пометками;
стр. 2 — таблица ошибок; стр. 3 — критерии ФИПИ и рекомендации. Данные — нормализованный разбор
essaycheck._result(); пустые блоки (missing) показываются честной пометкой, ничего не додумывается.
Зависимости: stdlib + reportlab (как у chatfiles). Ничего не знает про aiohttp/aiogram — проверяется
отдельным тестом (tests/test_essay_report.py).

ДВИЖКИ (ESSAY_REPORT_ENGINE, по умолчанию html):
  html      — отчёт верстается тем же HTML/CSS, что макет (assets/essay-report.css — стили макета дословно,
              assets/essay-report.js — раскладка по страницам A4), PDF печатает Chromium (playwright): это
              точная копия макета. Word — те же страницы A4 картинками на всю страницу: другого способа
              получить в Word «ровно как в HTML» нет (текст страниц — в альт-тексте картинок; сам текст работы
              после страниц идёт НАСТОЯЩИМ текстом приложением — см. _docx_appendix).
  reportlab — прежний запасной путь (PDF рисуется вручную, Word — текстовый). Включается сам, если Chromium
              не запустился: отчёт не должен ронять проверку.
"""
import base64, html as html_lib, io, json, logging, os, pathlib, re, tempfile, threading, zipfile
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
    """(класс карточки, подпись) — правило фронта (ecFipiView) и макета: 100% — полный балл, от 50% — частично
    (w), меньше половины — основная потеря (r). Ровно половина (1 из 2) — «частично», как в макете."""
    got, mx = c["got"], c["max"]
    pc = int(got * 100 / mx + 0.5) if mx else 0
    if pc >= 100:
        return "", "полный балл"
    return ("w", "частично") if pc >= 50 else ("r", "основная потеря")


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


# ---------- HTML-движок: точная копия макета (HTML/CSS → Chromium → PDF; Word — страницы A4 картинками) ----------
ENGINE = os.getenv("ESSAY_REPORT_ENGINE", "html").strip().lower()
ASSETS = os.path.join(HERE, "assets")
CSS_FILE = os.path.join(ASSETS, "essay-report.css")        # стили макета ai-check-a4-preview.html — дословно
JS_FILE = os.path.join(ASSETS, "essay-report.js")          # раскладка блоков по страницам A4
NUNITO = os.path.join(ASSETS, "fonts", "Nunito.ttf")       # шрифт макета (variable, OFL); нет файла — Google Fonts
SHORT = {"ege": "сочинение ЕГЭ", "oge": "эссе ОГЭ"}
SUB1 = "Разбор подготовила Вишенка"
THEAD = "<thead><tr><th>№</th><th>Фрагмент и исправление</th><th>Правило и источник</th></tr></thead>"
CSS_CLS = {"Пунктуация": " pu", "Речь": " re"}              # орфография — без класса, как в макете
GAUGE_L = 276.46                                           # длина окружности шкалы (2π·44): 12 из 17 → 195, как в макете
GAUGE_C = 277                                              # второй член dasharray — как в макете
_RENDER_LOCK = threading.Lock()                            # один Chromium за раз: на 512 МБ параллельные запуски не нужны


def _e(t):
    return html_lib.escape("" if t is None else str(t), quote=True)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _logo_uri():
    try:
        with open(LOGO, "rb") as f:
            return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
    except OSError:
        return ""


def _fonts_head():
    """(<link> для головы, @font-face). DejaVu из fonts/ всегда — кириллица не превратится в рамки;
    Nunito — из assets/fonts/Nunito.ttf, а если файла нет — тем же <link> на Google Fonts, что в макете."""
    uri = lambda p: pathlib.Path(p).resolve().as_uri()
    css = (f"@font-face{{font-family:'DejaVu Sans';src:url('{uri(os.path.join(chatfiles.FONTS, 'DejaVuSans.ttf'))}');font-weight:400}}"
           f"@font-face{{font-family:'DejaVu Sans';src:url('{uri(os.path.join(chatfiles.FONTS, 'DejaVuSans-Bold.ttf'))}');font-weight:700 900}}")
    if os.path.exists(NUNITO):
        return "", css + f"@font-face{{font-family:'Nunito';src:url('{uri(NUNITO)}');font-weight:200 1000}}"
    return ('<link rel="preconnect" href="https://fonts.googleapis.com">'
            '<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@600;700;800;900&display=swap" rel="stylesheet">'), css


def _pct(n):
    return min(100, int(n * 17.5 + 0.5))                   # ширины полос статистики — как ecStats фронта (4/2/1 → 70/35/18)


def _blocks(r, kind, student, now):
    """Разбор → список блоков для раскладки по страницам (разметка — дословно из макета)."""
    kind = _kind(kind)
    sc, miss = r.get("score") or {}, r.get("missing") or []
    errs, crits, recs = r.get("errors") or [], r.get("criteria") or [], r.get("recommendations") or []
    B = []

    def add(h, w=None, k=0, n=0):
        B.append({"h": h, "w": w, "k": k, "n": n})

    def empty(t):
        return f'<div class="empty">{_e(t)}</div>'

    def tag(t):
        return f"<span>{_e(t)}</span>" if t else ""

    # --- страница 1: шапка, балл, статистика, текст ---
    add(f"<h1>{_e(_title(kind))}</h1>", k=1)
    add(f'<p class="sub">{_e(WORK[kind][1])}</p>')
    if "criteria" in miss:
        add(empty(MISSING["criteria"]))
    else:
        got, mx, pot, c = sc.get("got", 0), sc.get("max", 0), potential(r), _counts(r)
        fill = max(0, min(1, got / mx)) * GAUGE_L if mx else 0
        arc = (f'<circle cx="52" cy="52" r="44" fill="none" stroke="#0E2250" stroke-width="9" stroke-linecap="round" '
               f'stroke-dasharray="{fill:.0f} {GAUGE_C}"/>') if fill >= 1 else ""
        who = (f'<div class="who"><span class="av">{_e(student.strip()[:1].upper())}</span>{_e(student)}</div>'
               if student.strip() else "")
        info = (f"Критериев: {len(crits)}<br>Ошибок найдено: {len(errs)}"
                + (f"<br>Потенциал: +{pot} {_plural(pot)}" if pot else ""))
        tiles = ('<div class="tiles">' + "".join(f"<div><b>{c[t]}</b>{t.lower()}</div>" for t in TYPES) + "</div>"
                 if "errors" not in miss else "")
        add('<div class="hero"><div class="r1"><div class="gauge">'
            '<svg width="98" height="98" viewBox="0 0 104 104"><circle cx="52" cy="52" r="44" fill="none" stroke="#C9CEDD" '
            f'stroke-width="9"/>{arc}</svg><div><b>{got}</b><small>из {mx}</small></div></div>'
            f'<div><div class="lb">Прогнозный балл</div><span class="lv">{_e(sc.get("level", ""))} · {_e(sc.get("percent", 0))}%</span>'
            f'{who}</div><div class="info">{info}</div></div>{tiles}</div>')
    if "errors" not in miss:
        c = _counts(r)
        add("<h2>Статистика ошибок</h2>", k=1)
        for t in TYPES:
            add(f'<div class="st"><span>{t}</span><div class="bar"><i style="width:{_pct(c[t])}%"></i></div><em>{c[t]}</em></div>')
    add("<h2>Текст работы с пометками</h2>", k=1)
    if "text" in miss:
        add(empty(MISSING["text"]))
    else:
        paras = _paras(r.get("text"), r.get("errors"))
        etype = {e["i"]: e["type"] for e in errs}
        for j, para in enumerate(paras):
            x = "".join((f'<span class="e{CSS_CLS.get(etype.get(i), "")}">{_e(s)}<sup>{_e(i)}</sup></span>' if i else _e(s))
                        for s, i in para)
            add(f"<p>{x}</p>", w="essay", k=1 if j == len(paras) - 1 else 0)
        add('<div class="legend"><span><i style="border-top:2px solid #000"></i>Орфография</span>'
            '<span><i style="border-top:2px dashed #000"></i>Пунктуация</span>'
            '<span><i style="border-top:3px double #000"></i>Речь</span>'
            '<span style="margin-left:auto">Текст распознан с фото, ошибки ученика сохранены</span></div>')

    # --- страница 2: таблица ошибок ---
    err_tag = f"{len(errs)} подтверждены текстом" if errs else ""
    add(f"<h2>Разбор ошибок{tag(err_tag)}</h2>", k=1, n=1)
    if not errs:
        add(empty(MISSING["errors"] if "errors" in miss else "Ошибок не найдено."))
    for e in errs:
        cls = CSS_CLS.get(e["type"], "")
        src = f'<span class="src">{_e(e["src"])}</span>' if e.get("src") else ""
        add(f'<tr><td><span class="num{cls}">{_e(e["i"])}</span></td><td><span class="pill{cls}">{_e(e["type"])}</span>'
            f'<div class="fix"><s>{_e(e["found"])}</s><span class="ar">→</span><b>{_e(e["correct"] or "—")}</b></div></td>'
            f'<td>{_e(e["why"])}{src}</td></tr>', w="tbl")

    # --- страница 3: критерии ФИПИ и рекомендации ---
    crit_tag = f"{sc.get('got', 0)} из {sc.get('max', 0)}" if crits else ""
    add(f"<h2>Критерии ФИПИ с обоснованием{tag(crit_tag)}</h2>", k=1, n=1)
    if not crits:
        add(empty(MISSING["criteria"]))
    for c in crits:
        cls, lab = _crit_state(c)
        pc = int(c["got"] * 100 / c["max"] + 0.5) if c["max"] else 0
        why = f'<p><b>Почему:</b> {_e(c["why"])}</p>' if c.get("why") else ""
        tip = f'<p class="tp"><b>Совет:</b> {_e(c["tip"])}</p>' if c.get("tip") else ""
        add(f'<div class="kc{(" " + cls) if cls else ""}"><div class="t"><span>{_e(c["name"])}</span>'
            f'<span class="p">{c["got"]} / {c["max"]} · {lab}</span></div><div class="b"><i style="width:{pc}%"></i></div>{why}{tip}</div>')
    if recs:
        pot = potential(r)
        pot_tag = f"потенциал +{pot} {_plural(pot)}" if pot else ""
        add(f"<h2>Рекомендации{tag(pot_tag)}</h2>", k=1)
        for n, x in enumerate(recs, 1):
            text = f'<p>{_e(x["text"])}</p>' if x.get("text") else ""
            badge = f'<em>{_e(x["badge"])}</em>' if x.get("badge") else ""
            add(f'<div class="rc"><div class="n">{n}</div><div><b>{_e(x["title"])}</b>{text}{badge}</div></div>', w="rcs")
    add(f'<div class="note">{_e(NOTE)}</div>')
    return B


def html_report(r, kind="ege", student="", now=None):
    """Разбор → самодостаточный HTML (стили макета + раскладка по страницам A4). Тот же файл можно открыть в браузере."""
    now, kind = now or datetime.now(), _kind(kind)
    student = (student or "").strip()
    cfg = {"brand": BRAND, "sub1": SUB1, "sub2": f"{student} · {SHORT[kind]}" if student else SHORT[kind],
           "date": _date(now), "num": _number(now), "logo": _logo_uri(), "thead": THEAD,
           "blocks": _blocks(r, kind, student, now)}
    data = json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/")
    link, face = _fonts_head()
    return ('<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
            f"<title>{_e(_title(kind))}</title>{link}<style>{face}\n{_read(CSS_FILE)}</style></head>"
            f'<body><div class="pages" id="pages"></div><script>window.REPORT={data};</script>'
            f"<script>{_read(JS_FILE)}</script></body></html>")


def render_pdf(html, timeout=45):
    """HTML → (PDF-байты, тексты страниц). Chromium через playwright; любой сбой — исключение (build уйдёт на запасной путь)."""
    from playwright.sync_api import sync_playwright
    with _RENDER_LOCK, tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "report.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        with sync_playwright() as p:
            br = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            try:
                pg = br.new_page()
                pg.goto(pathlib.Path(path).as_uri(), wait_until="load", timeout=timeout * 1000)
                pg.wait_for_function("window.__pages !== undefined", timeout=timeout * 1000)
                texts = pg.evaluate("window.__pages")
                pdf = pg.pdf(format="A4", print_background=True, prefer_css_page_size=True,
                             margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
            finally:
                br.close()
    return pdf, texts


def pdf_pages_png(pdf, dpi=200):
    """Страницы готового PDF → PNG (pypdfium2): Word получает ровно то, что напечатано в PDF."""
    import pypdfium2 as pdfium
    out = []
    for page in pdfium.PdfDocument(pdf):
        im = page.render(scale=dpi / 72).to_pil().convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        out.append(buf.getvalue())
    if not out:
        raise ValueError("в PDF нет страниц")
    return out


def _docx_appendix(r):
    """Приложение к страницам-картинкам: текст работы НАСТОЯЩИМ текстом Word (копируется, ищется, правится)
    с теми же пометками ошибок [N]. Без него Word приходит «без текста работы»: в HTML-движке текст живёт
    только в альт-тексте картинок, а для Word это не текст документа."""
    out = ['<w:p><w:pPr><w:pageBreakBefore/><w:spacing w:before="0" w:after="160"/></w:pPr>'
           '<w:r><w:rPr><w:b/><w:sz w:val="28"/></w:rPr>'
           '<w:t xml:space="preserve">Приложение: текст работы</w:t></w:r></w:p>']
    for para in _paras(r.get("text"), r.get("errors")):
        runs = ""
        for s, i in para:
            runs += (f'<w:r><w:rPr><w:u w:val="single"/></w:rPr><w:t xml:space="preserve">{escape(s)}</w:t></w:r>'
                     f'<w:r><w:rPr><w:b/><w:vertAlign w:val="superscript"/></w:rPr><w:t>[{i}]</w:t></w:r>') if i \
                else f'<w:r><w:t xml:space="preserve">{escape(s)}</w:t></w:r>'
        out.append(f'<w:p><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr>{runs}</w:p>')
    out.append('<w:p><w:pPr><w:spacing w:before="120"/></w:pPr><w:r><w:rPr><w:i/><w:sz w:val="16"/></w:rPr>'
               '<w:t xml:space="preserve">Подчёркнуто — найденные ошибки, [N] — номер в списке разбора. '
               'Текст распознан с фото, ошибки ученика сохранены.</w:t></w:r></w:p>')
    return "".join(out)


def docx_pages(pngs, texts, title, r=None):
    """Word: каждая страница A4 — картинка на всю страницу (поля 0). Картинка чуть ниже листа (295 из 297 мм),
    иначе Word добавит пустую страницу из-за знака абзаца. Текст страницы — в альт-тексте картинки, а переданный
    разбор r добавляет после страниц приложение с текстом работы настоящим текстом (см. _docx_appendix)."""
    NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
          'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
    cy = 295 * 36000
    cx = round(cy * 210 / 297)
    body, rels = [], []
    for i, _png in enumerate(pngs, 1):
        alt = escape(re.sub(r"\s+", " ", (texts[i - 1] if i <= len(texts) else "")).strip()[:1500], {'"': "&quot;"})
        body.append(
            '<w:p><w:pPr>' + ('<w:pageBreakBefore/>' if i > 1 else '') +
            '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/><w:jc w:val="center"/></w:pPr>'
            '<w:r><w:rPr><w:sz w:val="2"/></w:rPr><w:drawing>'
            f'<wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
            f'<wp:docPr id="{i}" name="Страница {i}" descr="{alt}"/>'
            '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
            f'<pic:nvPicPr><pic:cNvPr id="{i}" name="page{i}.png"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="rId{10 + i}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>'
            '</wp:inline></w:drawing></w:r></w:p>')
        rels.append(f'<Relationship Id="rId{10 + i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                    f'Target="media/page{i}.png"/>')
    if r is not None and (r.get("text") or "").strip():
        body.append(_docx_appendix(r))       # текст работы настоящим текстом — иначе Word «без текста»
    X = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    doc = (X + f'<w:document {NS}><w:body>' + "".join(body) +
           '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="0" w:right="0" w:bottom="0" w:left="0" w:header="0" w:footer="0" w:gutter="0"/></w:sectPr>'
           '</w:body></w:document>')
    ct = (X + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
          '</Types>')
    R = "http://schemas.openxmlformats.org/"
    root_rels = (X + f'<Relationships xmlns="{R}package/2006/relationships">'
                 f'<Relationship Id="rId1" Type="{R}officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                 f'<Relationship Id="rId2" Type="{R}package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                 '</Relationships>')
    doc_rels = X + f'<Relationships xmlns="{R}package/2006/relationships">' + "".join(rels) + "</Relationships>"
    core = (X + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>' + escape(title) + f"</dc:title><dc:creator>{escape(BRAND)}</dc:creator>"
            "</cp:coreProperties>")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("docProps/core.xml", core)
        for i, png in enumerate(pngs, 1):
            z.writestr(f"word/media/page{i}.png", png)
    return out.getvalue()


def build(r, kind="ege", student="", now=None):
    """Разбор → {"pdf": (байты, имя), "docx": (байты, имя)}. Формат, который не собрался, пропускается
    (в лог — причина): отчёт не должен ронять проверку, а один формат — лишать второго.
    Движок html: PDF печатает Chromium из макета, Word — те же страницы картинками. Chromium не запустился —
    запасной путь reportlab (PDF) и текстовый Word, отчёт всё равно уходит."""
    now = now or datetime.now()
    stem, out = file_name(kind, student, now), {}
    if ENGINE == "html":
        try:
            pdf, texts = render_pdf(html_report(r, kind, student, now))
            out["pdf"] = (pdf, f"{stem}.pdf")
            try:
                out["docx"] = (docx_pages(pdf_pages_png(pdf), texts, _title(kind), r), f"{stem}.docx")
            except Exception as e:
                log.exception("essayreport: Word из страниц PDF не собрался: %r", e)
        except Exception as e:
            log.warning("essayreport: HTML-движок (Chromium) не сработал, запасной путь reportlab: %r", e)
    for ext, fn in (("pdf", pdf_report), ("docx", docx_report)):
        if ext in out:
            continue
        try:
            out[ext] = (fn(r, kind, student, now), f"{stem}.{ext}")
        except Exception as e:
            log.exception("essayreport: %s не собрался: %r", ext, e)
    # диагностика доставки: что собралось и каким движком — без неё «PDF не доходит» не отлаживается
    if out:
        log.info("essayreport: отчёт собран (движок %s): %s", ENGINE,
                 ", ".join(f"{ext} {len(data) // 1024} КБ" for ext, (data, _) in sorted(out.items())))
    lost = [ext for ext in ("pdf", "docx") if ext not in out]
    if lost:
        log.warning("essayreport: отчёт НЕПОЛНЫЙ (движок %s): нет %s", ENGINE, ", ".join(lost))
    return out


def complete(files):
    """Сборка годится для кэша только полная: оба формата и оба файла непустые. Неполная или упавшая сборка
    в кэше = «отравленный кэш»: следующие запросы не увидят недостающий формат и не соберут его заново."""
    return (isinstance(files, dict) and set(files) == {"pdf", "docx"}
            and all(isinstance(f, tuple) and len(f) == 2 and isinstance(f[0], (bytes, bytearray)) and f[0]
                    and isinstance(f[1], str) and f[1] for f in files.values()))


def cache_key(uid, r, kind="ege", student=""):
    """Ключ сборки отчёта: один и тот же разбор (содержательная часть) + тип работы + ученик + владелец.
    По нему Telegram-отправка и кнопки «Переслать» на экране превью берут ОДИН собранный файл, а не два."""
    import hashlib
    keep = {"u": uid, "k": _kind(kind), "s": (student or "").strip(), "t": (r.get("text") or "").strip(),
            "e": [[e.get("type"), e.get("found"), e.get("correct"), e.get("why"), e.get("src")] for e in r.get("errors") or []],
            "c": [[c.get("name"), c.get("got"), c.get("max"), c.get("why"), c.get("tip")] for c in r.get("criteria") or []],
            "r": [[x.get("title"), x.get("text"), x.get("badge")] for x in r.get("recommendations") or []],
            "m": sorted(r.get("missing") or [])}
    return hashlib.sha1(json.dumps(keep, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def from_client(res, kind=""):
    """Разбор в том виде, как его держит экран превью (ecNrm) → нормализованный разбор essaycheck._result.
    Данным с клиента не доверяем: нумерация, баллы и типы ошибок пересчитываются на сервере, как при самой проверке.
    None — пересылать нечего (не словарь, слишком длинный текст, нет ни ошибок, ни критериев)."""
    import essaycheck
    try:
        if not isinstance(res, dict):
            return None
        text = res.get("text") if isinstance(res.get("text"), str) else ""
        task = res.get("task") if isinstance(res.get("task"), str) else ""
        miss = [m for m in (res.get("missing") if isinstance(res.get("missing"), list) else []) if isinstance(m, str)]
        if len(text) > 100_000:
            return None
        raw = {k: res.get(k) for k in ("errors", "criteria", "recommendations") if k not in miss}
        r = essaycheck._result(raw, text.strip(), task.strip()[:20000], kind=kind)
    except Exception as e:
        log.warning("essayreport.from_client: разбор не нормализовался: %r", e)
        return None
    return r if (r.get("errors") or r.get("criteria")) else None


def caption(r, kind="ege", student=""):
    """Подпись к файлам в Telegram: тип работы, ученик, итоговый балл."""
    sc = r.get("score") or {}
    head = _title(kind) + (f" · {student}" if student else "")
    if "criteria" in (r.get("missing") or []):
        return head
    return f"{head}\nПрогнозный балл: {sc.get('got', 0)} из {sc.get('max', 0)} · {sc.get('level', '')}"
