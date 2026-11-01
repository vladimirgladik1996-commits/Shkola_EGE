"""«ИИ-проверка»: отчёт о разборе сочинения в PDF (A4) и Word — для отправки в Telegram.

Сборка — тем же кодом, что и файлы «Чата с ИИ» (chatfiles): Word — chatfiles.docx_bytes, PDF — reportlab
с теми же шрифтами DejaVu (chatfiles._register_pdf_fonts). Вёрстка страниц — по макету
mockups/ai-check-a4-preview.html: стр. 1 — шапка, балл, статистика, текст работы с пометками;
стр. 2 — таблица ошибок; стр. 3 — критерии ФИПИ и рекомендации. Данные — нормализованный разбор
essaycheck._result(); пустые блоки (missing) показываются честной пометкой, ничего не додумывается.
Зависимости: stdlib + reportlab (как у chatfiles). Ничего не знает про aiohttp/aiogram — проверяется
отдельным тестом (tests/test_essay_report.py).

ДВИЖКИ (ESSAY_REPORT_ENGINE, по умолчанию html):
  html      — отчёт верстается тем же HTML/CSS, что макет (assets/essay-report.css — стили макета и дополнения,
              assets/essay-report.js — раскладка по страницам A4), PDF печатает Chromium (playwright).
              Word (ESSAY_DOCX, по умолчанию native) — РЕДАКТИРУЕМЫЙ документ, собранный кодом: таблицы, абзацы,
              колонтитулы «Стр. N из M», тот же порядок и цвета, что в PDF (docx_native). ESSAY_DOCX=pages —
              страницы PDF картинками на всю страницу (ровно как PDF, но не правится; текст работы после страниц
              идёт настоящим текстом — _docx_appendix).
  reportlab — прежний запасной путь (PDF рисуется вручную, Word — текстовый). Включается сам, если Chromium
              не запустился: отчёт не должен ронять проверку.
"""
import base64, html as html_lib, io, json, logging, os, pathlib, re, tempfile, threading, time, zipfile
from datetime import datetime
from xml.sax.saxutils import escape

import chatfiles

log = logging.getLogger("tutor.essayreport")

HERE = os.path.dirname(os.path.abspath(__file__))
LOGO = os.path.join(HERE, "assets", "essay-logo.jpg")      # аватар Вишенки из макета A4
BRAND = "Школа ЕГЭ · ИИ-проверка"
WORK = {"ege": ("Разбор сочинения ЕГЭ", "Русский язык · сочинение по прочитанному тексту"),
        "oge": ("Разбор эссе ОГЭ", "Русский язык · эссе для ОГЭ")}
TYPES = ("Орфография", "Пунктуация", "Грамматика", "Речь")
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
                    [(W - 24) / len(TYPES)] * len(TYPES),
                    [("BOX", (i, 0), (i, -1), 1.2, tx) for i in range(len(TYPES))]
                    + [("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]) \
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
CSS_CLS = {"Пунктуация": " pu", "Грамматика": " gr", "Речь": " re"}              # орфография — без класса, как в макете
def _short(name):
    """«К2 Комментарий» → «К2» (для плашек-«карты баллов»); без номера — название целиком."""
    m = re.match(r"\s*(К\s?\d+)", name or "")
    return m.group(1).replace(" ", "") if m else (name or "")


def summary(r):
    """Итоги проверки только из данных разбора (ничего не домысливается): карта баллов по критериям,
    критерии с полным баллом, критерии с потерями (по убыванию потерянных баллов), первые рекомендации."""
    crits, recs = r.get("criteria") or [], r.get("recommendations") or []
    return {"chips": [(_short(c["name"]), c["got"], c["max"], _crit_state(c)[0]) for c in crits],
            "strong": [c["name"] for c in crits if c["max"] and c["got"] >= c["max"]],
            "lost": sorted(((c["name"], c["max"] - c["got"]) for c in crits if c["got"] < c["max"]), key=lambda x: -x[1]),
            "next": [(x["title"], x.get("badge") or "") for x in recs[:3]]}


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


def _pct(n, mx=0):
    """Ширина полосы статистики, %. Шаг 17,5% на ошибку — как в макете (4/2/1 → 70/35/18); но когда ошибок много
    (самая длинная полоса дошла бы до края), шкала сжимается так, чтобы самая большая была 100%, а остальные —
    пропорционально меньше. Без этого при 6+ ошибках все полосы упирались в 100% и выглядели одинаково."""
    k = min(17.5, 100 / mx) if mx else 17.5
    return min(100, int(n * k + 0.5))


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
        arc = (f'<circle cx="52" cy="52" r="44" fill="none" stroke="#E3BC55" stroke-width="9" stroke-linecap="round" '
               f'stroke-dasharray="{fill:.0f} {GAUGE_C}"/>') if fill >= 1 else ""
        who = (f'<div class="who"><span class="av">{_e(student.strip()[:1].upper())}</span>{_e(student)}</div>'
               if student.strip() else "")
        info = (f"Критериев: {len(crits)}<br>Ошибок найдено: {len(errs)}"
                + (f"<br>Потенциал: +{pot} {_plural(pot)}" if pot else ""))
        tiles = ('<div class="tiles">' + "".join(f'<div><b>{c[t]}</b>{t.lower()}</div>' for t in TYPES) + "</div>"
                 if "errors" not in miss else "")
        add('<div class="hero"><div class="r1"><div class="gauge">'
            '<svg width="98" height="98" viewBox="0 0 104 104"><circle cx="52" cy="52" r="44" fill="none" stroke="#FFFFFF" stroke-opacity=".18" '
            f'stroke-width="9"/>{arc}</svg><div><b>{got}</b><small>из {mx}</small></div></div>'
            f'<div><div class="lb">Прогнозный балл</div><span class="lv">{_e(sc.get("level", ""))} · {_e(sc.get("percent", 0))}%</span>'
            f'{who}</div><div class="info">{info}</div></div>{tiles}</div>')
        if crits:
            sm = summary(r)
            add('<div class="chips">' + "".join(f'<span class="chip{(" " + st) if st else ""}"><b>{_e(n)}</b> {g}/{m}</span>'
                                              for n, g, m, st in sm["chips"]) + "</div>")
            more = f'<li class="no">и ещё {len(sm["strong"]) - 5}</li>' if len(sm["strong"]) > 5 else ""
            li_s = "".join(f"<li>{_e(n)}</li>" for n in sm["strong"][:5]) + more or '<li class="no">Полный балл пока ни по одному критерию</li>'
            li_l = "".join(f'<li>{_e(n)}<span class="loss">−{d}</span></li>' for n, d in sm["lost"][:5]) or '<li class="no">Баллы не потеряны</li>'
            li_n = "".join(f'<li><b>{_e(t)}</b>' + (f" <em>{_e(b)}</em>" if b else "") + "</li>" for t, b in sm["next"]) or '<li class="no">Рекомендации не сформированы</li>'
            add(f'<div class="sum"><div class="g"><h3>Сильные стороны</h3><ul>{li_s}</ul></div>'
                f'<div class="l"><h3>Где теряются баллы</h3><ul>{li_l}</ul></div>'
                f'<div class="n"><h3>С чего начать</h3><ul>{li_n}</ul></div></div>')
    if "errors" not in miss:
        c = _counts(r)
        add("<h2>Статистика ошибок</h2>", k=1)
        for t in TYPES:
            add(f'<div class="st"><span>{t}</span><div class="bar"><i style="width:{_pct(c[t], max(c.values()))}%"></i></div><em>{c[t]}</em></div>')
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
        add(f'<div class="legend"><span><i style="border-top:2px solid #0E2250"></i>Орфография</span>'
            f'<span><i style="border-top:2px dashed #0E2250"></i>Пунктуация</span>'
            f'<span><i style="border-top:2px dotted #0E2250"></i>Грамматика</span>'
            f'<span><i style="border-top:3px double #0E2250"></i>Речь</span>'
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


# ---------- Word: настоящий редактируемый документ, собранный кодом (таблицы, стили, колонтитулы) ----------
# Не картинка макета: весь текст — обычные абзацы и таблицы Word, правится как любой документ. Повторяет макет по
# смыслу и порядку (шапка, балл, плитки, статистика, текст с пометками, таблица ошибок, критерии, рекомендации),
# а не пиксель в пиксель: Word не умеет скругления, CSS и шрифт Nunito. Строгий порядок элементов OOXML —
# чтобы документ открывался не только в Word, но и в мобильных просмотрщиках.
DOCX_MODE = os.getenv("ESSAY_DOCX", "native").strip().lower()   # native — редактируемый; pages — страницы-картинки
WFONT = "Calibri"
NAVY, GREYBG, LNC, HINTC, INKC = "0E2250", "EDEDED", "8C93AA", "3F4A6E", "111111"
PG_W, PG_H, PG_M = 11906, 16838, 1134                      # A4 и поля 2 см, twips
MUTED, LINE, TRACK, BARC = "5F6880", "D5D8E3", "EEF0F6", "8794C0"   # приглушённый текст, волосяные линии, дорожка и светлая заливка полос
CW = PG_W - 2 * PG_M                                       # ширина текста страницы
_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
       'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
       'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_UL = {"": ("single", "single"), "Пунктуация": ("dash", "dashed"), "Речь": ("double", "double")}   # подчёркивание / рамка плашки


def _t(s):
    return escape(str(s), {'"': "&quot;"})


def _run(text, b=False, i=False, strike=False, color=None, sz=None, u=None, shd=None, sup=False, caps=False, bdr=None, sp=None, uc=None):
    """Фрагмент текста; порядок свойств — как в схеме OOXML (rPr)."""
    pr = ("<w:b/><w:bCs/>" if b else "") + ("<w:i/><w:iCs/>" if i else "") + ("<w:caps/>" if caps else "") + \
         ("<w:strike/>" if strike else "") + (f'<w:color w:val="{color}"/>' if color else "") + \
         (f'<w:spacing w:val="{sp}"/>' if sp else "") + (f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>' if sz else "") + \
         (f'<w:u w:val="{u}" w:color="{uc or "000000"}"/>' if u else "") + \
         (f'<w:bdr w:val="{bdr[0]}" w:sz="{bdr[1]}" w:space="0" w:color="{bdr[2]}"/>' if bdr else "") + \
         (f'<w:shd w:val="clear" w:color="auto" w:fill="{shd}"/>' if shd else "") + ('<w:vertAlign w:val="superscript"/>' if sup else "")
    return f'<w:r>{"<w:rPr>" + pr + "</w:rPr>" if pr else ""}<w:t xml:space="preserve">{_t(text)}</w:t></w:r>'


def _tab():
    return "<w:r><w:tab/></w:r>"


def _p(runs="", jc=None, before=0, after=0, line=None, exact=None, keep=False, brk=False, style=None, pbdr=None, tabs=None, ind=None, shd=None, hang=None):
    """Абзац; порядок свойств — как в схеме (pPr)."""
    pr = (f'<w:pStyle w:val="{style}"/>' if style else "") + ("<w:keepNext/>" if keep else "") + \
         ("<w:pageBreakBefore/>" if brk else "") + (f"<w:pBdr>{pbdr}</w:pBdr>" if pbdr else "") + \
         (f'<w:shd w:val="clear" w:color="auto" w:fill="{shd}"/>' if shd else "") + \
         (f"<w:tabs>{''.join(f'<w:tab w:val=\"{v}\" w:pos=\"{pos}\"/>' for v, pos in tabs)}</w:tabs>" if tabs else "") + \
         f'<w:spacing w:before="{before}" w:after="{after}"' + \
         (f' w:line="{exact}" w:lineRule="exact"' if exact else f' w:line="{line}" w:lineRule="auto"' if line else "") + "/>" + \
         (f'<w:ind w:left="{ind}" w:hanging="{hang}"/>' if hang else f'<w:ind w:left="{ind}"/>' if ind else "") + (f'<w:jc w:val="{jc}"/>' if jc else "")
    return f"<w:p><w:pPr>{pr}</w:pPr>{runs}</w:p>"


def _bd(side, val="single", sz=8, color=LNC, space=0):
    return f'<w:{side} w:val="{val}" w:sz="{sz}" w:space="{space}" w:color="{color}"/>'


def _tc(inner, w, shd=None, borders="", span=None, va=None, mar=None):
    """Ячейка (tcPr в порядке схемы). В конце ячейки всегда абзац — иначе Word считает файл повреждённым."""
    pr = f'<w:tcW w:w="{w}" w:type="dxa"/>' + (f'<w:gridSpan w:val="{span}"/>' if span else "") + \
         (f"<w:tcBorders>{borders}</w:tcBorders>" if borders else "") + \
         (f'<w:shd w:val="clear" w:color="auto" w:fill="{shd}"/>' if shd else "") + \
         (f'<w:tcMar><w:top w:w="{mar[0]}" w:type="dxa"/><w:left w:w="{mar[1]}" w:type="dxa"/><w:bottom w:w="{mar[0]}" w:type="dxa"/>'
          f'<w:right w:w="{mar[1]}" w:type="dxa"/></w:tcMar>' if mar else "") + (f'<w:vAlign w:val="{va}"/>' if va else "")
    if not inner.rstrip().endswith("</w:p>"):
        inner += _p()
    return f"<w:tc><w:tcPr>{pr}</w:tcPr>{inner}</w:tc>"


def _tr(cells, split=False, head=False, h=None):
    pr = ("<w:cantSplit/>" if not split else "") + (f'<w:trHeight w:val="{h}" w:hRule="exact"/>' if h else "") + ("<w:tblHeader/>" if head else "")
    return f'<w:tr>{"<w:trPr>" + pr + "</w:trPr>" if pr else ""}{"".join(cells)}</w:tr>'


def _tbl(rows, widths, borders="", spacing=None, mar=None, jc=None):
    """Таблица фиксированной ширины (tblPr в порядке схемы)."""
    pr = f'<w:tblW w:w="{sum(widths)}" w:type="dxa"/>' + (f'<w:jc w:val="{jc}"/>' if jc else "") + \
         (f'<w:tblCellSpacing w:w="{spacing}" w:type="dxa"/>' if spacing else "") + \
         (f"<w:tblBorders>{borders}</w:tblBorders>" if borders else "") + '<w:tblLayout w:type="fixed"/>' + \
         (f'<w:tblCellMar><w:top w:w="{mar[0]}" w:type="dxa"/><w:left w:w="{mar[1]}" w:type="dxa"/><w:bottom w:w="{mar[0]}" w:type="dxa"/>'
          f'<w:right w:w="{mar[1]}" w:type="dxa"/></w:tblCellMar>' if mar else "") + '<w:tblLook w:val="0000"/>'
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    return f'<w:tbl><w:tblPr>{pr}</w:tblPr><w:tblGrid>{grid}</w:tblGrid>{"".join(rows)}</w:tbl>'


def _box(side_all=True, val="single", sz=12, color=LNC):
    return "".join(_bd(s, val, sz, color) for s in ("top", "left", "bottom", "right"))


def _spacer(h=100):
    """Абзац-разделитель: две таблицы подряд Word склеивает в одну."""
    return _p(exact=h)


def _hdr_ftr(student, kind, now, first):
    """Колонтитулы: слева бренд и подпись, справа дата и номер; в подвале «Стр. N из M» полями Word. Только текст и тонкие линии."""
    sub = SUB1 if first else (f"{student} · {SHORT[kind]}" if student else SHORT[kind])
    wm = 3000
    brand = _tc(_p(_run(BRAND, b=True, sz=21, color=NAVY)) + _p(_run(sub, sz=16, color=MUTED)), CW - wm, va="bottom", mar=(40, 0))
    meta = _tc(_p(_run(_date(now), sz=16, color=MUTED), jc="right") + _p(_run("Отчёт № " + _number(now), sz=16, color=MUTED), jc="right"),
               wm, va="bottom", mar=(40, 0))
    hdr = _tbl([_tr([brand, meta])], [CW - wm, wm], borders=_bd("bottom", "single", 8, NAVY, 4))
    head = _XML + f"<w:hdr {_NS}>{hdr}{_p(exact=60)}</w:hdr>"
    fld = lambda ins, v: (f'<w:fldSimple w:instr=" {ins} "><w:r><w:rPr><w:color w:val="{MUTED}"/><w:sz w:val="16"/><w:szCs w:val="16"/></w:rPr><w:t>{v}</w:t></w:r></w:fldSimple>')
    foot = _XML + (f"<w:ftr {_NS}>" + _p(_run(BRAND, sz=16, color=MUTED) + _tab() + _run("Стр. ", sz=16, color=MUTED) + fld("PAGE", "1") +
                                         _run(" из ", sz=16, color=MUTED) + fld("NUMPAGES", "1"),
                                         pbdr=_bd("top", "single", 6, LINE, 4), tabs=[("right", CW)], before=60) + "</w:ftr>")
    return head, foot


def _h2(text, tag="", brk=False):
    """Заголовок раздела: тёмно-синий, снизу тонкая линия; справа приглушённая подпись-счётчик."""
    runs = _run(text, b=True, sz=25, color=NAVY)
    if tag:
        runs += _tab() + _run(tag, sz=17, color=MUTED)
    return _p(runs, style="Heading2", keep=True, brk=brk, before=240, after=100, tabs=[("right", CW)], pbdr=_bd("bottom", "single", 6, LINE, 4))


def _empty_box(text):
    return _tbl([_tr([_tc(_p(_run(text, sz=20, color=MUTED), jc="center"), CW, mar=(120, 120))])], [CW],
                borders=_box(val="dashed", sz=6, color=LNC)) + _spacer(80)


def _stat_tbl(label, n, pct):
    """Статистика: подпись и число над тонкой полосой. Плоская таблица, без вложенных (мобильные просмотрщики их не любят)."""
    f = round(CW * max(0, min(100, pct)) / 100)
    cols = [w for w in (f, CW - f) if w]
    bar = ([_tc(_p(exact=20), f, shd=BARC)] if f else []) + ([_tc(_p(exact=20), CW - f, shd=TRACK)] if CW - f else [])
    head = _tc(_p(_run(label, sz=20, color=INKC) + _tab() + _run(str(n), b=True, sz=20, color=NAVY), tabs=[("right", CW)]),
               CW, span=len(cols) if len(cols) > 1 else None, mar=(20, 0))
    return _tbl([_tr([head]), _tr(bar, h=22)], cols)


def docx_native(r, kind="ege", student="", now=None):
    """Разбор → редактируемый Word (.docx): настоящий текст, таблицы, стили и колонтитулы «Стр. N из M». Ни одной картинки.
    Лёгкая вёрстка «как набранный отчёт»: белый лист, тонкие линии, акцент — тёмно-синий; различие типов ошибок и состояний
    критериев — стилями линий (сплошная / пунктир / двойная), поэтому читается и в чёрно-белой печати и почти не расходует тонер."""
    now, kind, student = now or datetime.now(), _kind(kind), (student or "").strip()
    sc, miss = r.get("score") or {}, r.get("missing") or []
    errs, crits, recs = r.get("errors") or [], r.get("criteria") or [], r.get("recommendations") or []
    body = []

    # --- заголовок и балл ---
    body.append(_p(_run(_title(kind), b=True, sz=44, color=NAVY), style="Heading1", keep=True, after=40))
    body.append(_p(_run(WORK[kind][1], sz=20, color=MUTED), after=160))
    if "criteria" in miss:
        body.append(_empty_box(MISSING["criteria"]))
    else:
        got, mx, pot, c = sc.get("got", 0), sc.get("max", 0), potential(r), _counts(r)
        rw = 2900
        left = (_p(_run("Прогнозный балл", b=True, sz=16, color=MUTED, caps=True, sp=24)) +
                _p(_run(str(got), b=True, sz=72, color=NAVY) + _run(f"  из {mx}", sz=28, color=MUTED) + _run("        ") +
                   _run(f"{sc.get('level', '')} · {sc.get('percent', 0)}%", b=True, sz=28, color=NAVY), line=240, before=40) +
                (_p(_run(student, b=True, sz=21, color=INKC), before=40) if student else ""))
        right = (_p(_run(f"Критериев: {len(crits)}", sz=18, color=MUTED), jc="right", line=300) +
                 _p(_run(f"Ошибок найдено: {len(errs)}", sz=18, color=MUTED), jc="right", line=300) +
                 (_p(_run(f"Потенциал: +{pot} {_plural(pot)}", b=True, sz=18, color=NAVY), jc="right", line=300) if pot else ""))
        body.append(_tbl([_tr([_tc(left, CW - rw, mar=(120, 0)), _tc(right, rw, va="bottom", mar=(120, 0))])], [CW - rw, rw],
                         borders=_bd("top", "single", 12, NAVY) + _bd("bottom", "single", 6, LINE)))
        if "errors" not in miss:
            body.append(_spacer(130))
            tw = CW // len(TYPES)
            tile = lambda t, k: _tc(_p(_run(str(c[t]), b=True, sz=44, color=NAVY), jc="center") + _p(_run(t.lower(), sz=17, color=MUTED), jc="center"),
                                    tw if k < len(TYPES) - 1 else CW - tw * (len(TYPES) - 1), mar=(40, 0), borders=(_bd("left", "single", 6, LINE) if k else ""))
            body.append(_tbl([_tr([tile(t, k) for k, t in enumerate(TYPES)])],
                             [tw] * (len(TYPES) - 1) + [CW - tw * (len(TYPES) - 1)]))
        body.append(_spacer(190))
        if crits:                                           # итоги — только из данных разбора — только из данных разбора
            sm = summary(r)
            gap = 300
            cw3 = (CW - 2 * gap) // 3
            HG = 190                                                      # висячий отступ: перенос строки встаёт под текст, а не под тире
            item = lambda runs, **kw: _p(_run("–", sz=19, color=LNC) + _tab() + runs, ind=HG, hang=HG, line=265, before=40, **kw)
            more = [item(_run(f"и ещё {len(sm['strong']) - 5}", sz=19, color=MUTED))] if len(sm["strong"]) > 5 else []
            c_s = "".join(item(_run(n_, sz=19, color=INKC)) for n_ in sm["strong"][:5]) + "".join(more) or _p(_run("Полный балл пока ни по одному критерию", sz=19, color=MUTED))
            c_l = "".join(item(_run(n_, sz=19, color=INKC) + _tab() + _run(f"−{d_}", b=True, sz=19, color=NAVY), tabs=[("left", HG), ("right", cw3)]) for n_, d_ in sm["lost"][:5]) \
                or _p(_run("Баллы не потеряны", sz=19, color=MUTED))
            c_n = "".join(item(_run(t_, b=True, sz=19, color=INKC)) + (_p(_run(b_, sz=16, color=MUTED), ind=HG, before=0) if b_ else "") for t_, b_ in sm["next"]) \
                or _p(_run("Рекомендации не сформированы", sz=19, color=MUTED))
            head = lambda t_: _p(_run(t_, b=True, sz=16, color=NAVY, caps=True, sp=20), after=60)
            card = lambda inner, w: _tc(inner, w, mar=(80, 0), borders=_bd("top", "single", 12, NAVY))
            body.append(_tbl([_tr([card(head("Сильные стороны") + c_s, cw3), _tc(_p(), gap), card(head("Где теряются баллы") + c_l, cw3), _tc(_p(), gap),
                                   card(head("С чего начать") + c_n, CW - 2 * cw3 - 2 * gap)])], [cw3, gap, cw3, gap, CW - 2 * cw3 - 2 * gap]))
    if "errors" not in miss:
        c = _counts(r)
        body.append(_h2("Статистика ошибок"))
        for t in TYPES:
            body.append(_stat_tbl(t, c[t], _pct(c[t], max(c.values()))))
            body.append(_spacer(60))

    # --- текст работы с пометками ---
    body.append(_h2("Текст работы с пометками"))
    if "text" in miss:
        body.append(_empty_box(MISSING["text"]))
    else:
        ps = []
        for para in _paras(r.get("text"), r.get("errors")):
            rr = ""
            etype = {e["i"]: e["type"] for e in errs}
            for s_, i in para:
                if i:
                    rr += _run(s_, sz=21, u=_UL.get(etype.get(i), _UL[""])[0], uc=NAVY) + _run(str(i), b=True, sz=14, color=NAVY, sup=True)
                else:
                    rr += _run(s_, sz=21)
            ps.append(_p(rr, line=320, after=70))
        body.append(_p(_run("Орфография", sz=16, color=MUTED, u="single", uc=NAVY) + _run("     ") + _run("Пунктуация", sz=16, color=MUTED, u="dash", uc=NAVY) +
                       _run("     ") + _run("Речь", sz=16, color=MUTED, u="double", uc=NAVY) + _tab() +
                       _run("Текст распознан с фото, ошибки ученика сохранены", sz=16, color=MUTED), after=70, tabs=[("right", CW)], keep=True))
        body.append(_tbl([_tr([_tc("".join(ps), CW, mar=(130, 220))], split=True)], [CW], borders=_box(sz=6, color=LINE)))

    # --- разбор ошибок ---
    body.append(_h2("Разбор ошибок", f"{len(errs)} подтверждены текстом" if errs else ""))
    if not errs:
        body.append(_empty_box(MISSING["errors"] if "errors" in miss else "Ошибок не найдено."))
    else:
        w1, w3 = 520, 3000
        w2 = CW - w1 - w3
        th = lambda t, w, m: _tc(_p(_run(t, b=True, sz=15, color=MUTED, caps=True, sp=16)), w, borders=_bd("bottom", "single", 8, NAVY), mar=(50, m))
        rows = [_tr([th("№", w1, 0), th("Фрагмент и исправление", w2, 100), th("Правило и источник", w3, 100)], head=True)]
        line_ = _bd("bottom", "single", 4, LINE)
        for e in errs:
            ul = _UL.get(e["type"], _UL[""])[0]
            fix = _run(e["found"], sz=22, color=MUTED, strike=True) + _run("   →   ", sz=22, color=LNC) + _run(e["correct"] or "—", b=True, sz=22, color=NAVY)
            why = _p(_run(e["why"], sz=19, color=INKC), line=270) + (_p(_run(e["src"], sz=16, color=MUTED), before=30) if e.get("src") else "")
            rows.append(_tr([
                _tc(_p(_run(str(e["i"]), b=True, sz=22, color=NAVY)), w1, borders=line_, mar=(90, 0)),
                _tc(_p(_run(e["type"], sz=16, color=MUTED, u=ul, uc=NAVY), after=40) + _p(fix), w2, borders=line_, mar=(90, 100)),
                _tc(why, w3, borders=line_, mar=(90, 100))]))
        body.append(_tbl(rows, [w1, w2, w3]))

    # --- критерии и рекомендации ---
    body.append(_h2("Критерии ФИПИ с обоснованием", f"{sc.get('got', 0)} из {sc.get('max', 0)}" if crits else ""))
    if not crits:
        body.append(_empty_box(MISSING["criteria"]))
    for c in crits:
        cls, lab = _crit_state(c)
        pc = int(c["got"] * 100 / c["max"] + 0.5) if c["max"] else 0
        state = _run("   ") + _run(lab, sz=16, b=(cls == "r"), color=NAVY if cls == "r" else MUTED)
        title = _p(_run(c["name"], b=True, sz=21, color=INKC) + _tab() + _run(f"{c['got']} / {c['max']}", b=True, sz=21, color=NAVY) + state,
                   tabs=[("right", CW)], keep=True)
        text = ""
        if c.get("why"):
            text += _p(_run("Почему: ", b=True, sz=19, color=INKC) + _run(c["why"], sz=19, color=INKC), line=270)
        if c.get("tip"):
            text += _p(_run("Совет: ", b=True, sz=19, color=MUTED) + _run(c["tip"], sz=19, color=MUTED), line=270, before=30)
        body.append(_tbl([_tr([_tc(title, CW, mar=(90, 0))]),
                          _tr([_tc(text or _p(exact=20), CW, mar=(30, 0), borders=_bd("bottom", "single", 4, LINE))])], [CW]))
        body.append(_spacer(60))
    if recs:
        pot = potential(r)
        body.append(_h2("Рекомендации", f"потенциал +{pot} {_plural(pot)}" if pot else ""))
        nw = 520
        rows = []
        for n, x in enumerate(recs, 1):
            txt = _p(_run(x["title"], b=True, sz=21, color=INKC) + (_tab() + _run(x["badge"], sz=16, color=NAVY) if x.get("badge") else ""),
                     tabs=[("right", CW - nw)], keep=True)
            if x.get("text"):
                txt += _p(_run(x["text"], sz=19, color=MUTED), line=270, before=30)
            rows.append(_tr([_tc(_p(_run(str(n), b=True, sz=28, color=NAVY)), nw, borders=_bd("bottom", "single", 4, LINE), mar=(80, 0)),
                             _tc(txt, CW - nw, borders=_bd("bottom", "single", 4, LINE), mar=(80, 0))]))
        body.append(_tbl(rows, [nw, CW - nw]))
    body.append(_p(_run(NOTE, i=True, sz=16, color=MUTED), line=260, before=240, pbdr=_bd("top", "single", 4, LINE, 6)))

    hdr1, ftr1 = _hdr_ftr(student, kind, now, True)
    hdr2, ftr2 = _hdr_ftr(student, kind, now, False)
    sect = ('<w:sectPr><w:headerReference w:type="default" r:id="rId3"/><w:headerReference w:type="first" r:id="rId4"/>'
            '<w:footerReference w:type="default" r:id="rId5"/><w:footerReference w:type="first" r:id="rId6"/>'
            f'<w:pgSz w:w="{PG_W}" w:h="{PG_H}"/><w:pgMar w:top="1400" w:right="{PG_M}" w:bottom="1100" w:left="{PG_M}" w:header="560" w:footer="520" w:gutter="0"/>'
            '<w:cols w:space="708"/><w:titlePg/></w:sectPr>')
    doc = _XML + f'<w:document {_NS}><w:body>{"".join(body)}{sect}</w:body></w:document>'
    return _pack(doc, hdr1, hdr2, ftr1, ftr2, _title(kind), now)


_STYLES = (_XML + '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:docDefaults><w:rPrDefault><w:rPr>'
           f'<w:rFonts w:ascii="{WFONT}" w:hAnsi="{WFONT}" w:eastAsia="{WFONT}" w:cs="{WFONT}"/><w:sz w:val="20"/><w:szCs w:val="20"/>'
           '<w:lang w:val="ru-RU" w:eastAsia="ru-RU" w:bidi="ar-SA"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
           '<w:spacing w:after="0" w:line="252" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
           '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
           '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont"><w:name w:val="Default Paragraph Font"/><w:uiPriority w:val="1"/><w:semiHidden/></w:style>'
           '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:uiPriority w:val="99"/><w:semiHidden/>'
           '<w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/>'
           '<w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
           '<w:style w:type="numbering" w:default="1" w:styleId="NoList"><w:name w:val="No List"/><w:uiPriority w:val="99"/><w:semiHidden/></w:style>'
           '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
           '<w:pPr><w:keepNext/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:bCs/></w:rPr></w:style>'
           '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
           '<w:pPr><w:keepNext/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:bCs/></w:rPr></w:style></w:styles>')
_SETTINGS = (_XML + '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:zoom w:percent="100"/>'
             '<w:defaultTabStop w:val="720"/><w:characterSpacingControl w:val="doNotCompress"/>'
             '<w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>'
             '<w:themeFontLang w:val="ru-RU"/></w:settings>')


def _pack(doc, hdr1, hdr2, ftr1, ftr2, title, now):
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    ct = (_XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
          '<Default Extension="png" ContentType="image/png"/><Default Extension="jpeg" ContentType="image/jpeg"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
          '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
          + "".join(f'<Override PartName="/word/header{n}.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
                    f'<Override PartName="/word/footer{n}.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>' for n in (1, 2)) +
          '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
          '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>')
    root = (_XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{R}officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            f'<Relationship Id="rId3" Type="{R}extended-properties" Target="docProps/app.xml"/></Relationships>')
    drels = (_XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             f'<Relationship Id="rId1" Type="{R}styles" Target="styles.xml"/><Relationship Id="rId2" Type="{R}settings" Target="settings.xml"/>'
             f'<Relationship Id="rId3" Type="{R}header" Target="header2.xml"/><Relationship Id="rId4" Type="{R}header" Target="header1.xml"/>'
             f'<Relationship Id="rId5" Type="{R}footer" Target="footer2.xml"/><Relationship Id="rId6" Type="{R}footer" Target="footer1.xml"/>'
             + "</Relationships>")
    core = (_XML + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>{escape(title)}</dc:title><dc:creator>{escape(BRAND)}</dc:creator>'
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{iso}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{iso}</dcterms:modified></cp:coreProperties>')
    app = (_XML + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Shkola EGE</Application>'
           '<DocSecurity>0</DocSecurity></Properties>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", root)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", drels)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/settings.xml", _SETTINGS)
        for n, (h, f) in enumerate(((hdr1, ftr1), (hdr2, ftr2)), 1):
            z.writestr(f"word/header{n}.xml", h)
            z.writestr(f"word/footer{n}.xml", f)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
    return out.getvalue()


def build(r, kind="ege", student="", now=None):
    """Разбор → {"pdf": (байты, имя), "docx": (байты, имя)}. Формат, который не собрался, пропускается
    (в лог — причина): отчёт не должен ронять проверку, а один формат — лишать второго.
    PDF: движок html — Chromium печатает макет (точная копия); не запустился — запасной reportlab.
    Word: редактируемый документ, собранный кодом (docx_native — таблицы, абзацы, колонтитулы), не картинка;
    ESSAY_DOCX=pages — страницы макета картинками (точная копия, но не правится); сбой — прежний текстовый Word."""
    now = now or datetime.now()
    stem, out, texts = file_name(kind, student, now), {}, []
    t0 = time.monotonic()
    if ENGINE == "html":
        try:
            pdf, texts = render_pdf(html_report(r, kind, student, now))
            out["pdf"] = (pdf, f"{stem}.pdf")
        except Exception as e:
            log.warning("essayreport: HTML-движок (Chromium) не сработал, запасной путь reportlab: %r", e)
    if "pdf" not in out:
        try:
            out["pdf"] = (pdf_report(r, kind, student, now), f"{stem}.pdf")
        except Exception as e:
            log.exception("essayreport: pdf не собрался: %r", e)
    try:
        if DOCX_MODE == "pages" and "pdf" in out and texts:
            data = docx_pages(pdf_pages_png(out["pdf"][0]), texts, _title(kind), r)
        else:
            data = docx_native(r, kind, student, now)
        out["docx"] = (data, f"{stem}.docx")
    except Exception as e:
        log.exception("essayreport: Word не собрался штатно, запасной текстовый: %r", e)
        try:
            out["docx"] = (docx_report(r, kind, student, now), f"{stem}.docx")
        except Exception as e2:
            log.exception("essayreport: docx не собрался: %r", e2)
    # диагностика доставки: что собралось и каким движком — без неё «PDF не доходит» не отлаживается
    if out:
        log.info("essayreport: отчёт собран за %.1fs (движок %s): %s", time.monotonic() - t0, ENGINE,
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
