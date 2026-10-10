import asyncio, collections, hashlib, hmac, json, logging, os, re, sqlite3, time
from datetime import date, timedelta
from urllib.parse import parse_qsl

from aiohttp import web
import auth
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           MenuButtonWebApp, Message, ReplyKeyboardRemove, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tutor")

TOKEN = os.environ["BOT_TOKEN"]
# Кто допущен — см. auth.py: ALLOWED_USERNAMES (по умолчанию три @ника), ALLOWED_IDS, OWNER_ID (старый вариант, тоже работает).
URL = os.getenv("WEBAPP_URL", "")            # https-адрес мини-аппа
PORT = int(os.getenv("PORT", "8080"))
CUR = os.getenv("CURRENCY", "₽")
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WD_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
PER = 6
WEEKS_AHEAD = 12   # на сколько недель вперёд раскладываются регулярные занятия

# ---------- БД ----------
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")            # ./data/tutor.db — см. README и env/*.example
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "tutor.db")
_legacy_db = os.path.join(HERE, "tutor.db")      # старая база лежала рядом с кодом — переносим
if not os.path.exists(DB_PATH) and os.path.exists(_legacy_db):
    os.replace(_legacy_db, DB_PATH)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
try: os.chmod(DB_PATH, 0o600)
except OSError: pass
db.row_factory = sqlite3.Row
db.execute("pragma busy_timeout=5000")           # при конкурирующей записи ждём, а не падаем сразу
db.execute("pragma journal_mode=wal")            # устойчивее к сбоям; консистентная копия при бэкапе
db.execute("pragma synchronous=normal")

BASE_SCHEMA = """
create table if not exists students(id integer primary key, name text not null, price integer default 0, photo text);
create table if not exists lessons(id integer primary key, student_id integer not null, day text not null, time text not null,
  regular_id integer, dur integer default 60, unique(day,time));
create table if not exists payments(id integer primary key, student_id integer not null, amount integer not null,
  created text default (datetime('now','localtime')));
create table if not exists regular(id integer primary key, student_id integer not null, weekday integer not null,
  time text not null, start text not null, dur integer default 60);
create table if not exists skips(regular_id integer, day text, primary key(regular_id, day));
"""

# Версионируемые миграции (pragma user_version). Новая версия схемы = новый элемент списка; старые не правим.
# Каждая команда идемпотентна: «duplicate column» на базах, где колонка уже есть, пропускается.
MIGRATIONS = [
    [   # v1: колонки, добавлявшиеся в прошлых версиях (для старых баз)
        "alter table lessons add column regular_id integer",
        "alter table students add column photo text",
        "alter table lessons add column dur integer default 60",
        "alter table regular add column dur integer default 60",
        "alter table students add column grade text",
        "alter table students add column subject text",
        "alter table lessons add column price integer",       # цена, «замороженная» для уже прошедших занятий
        "alter table students add column deleted integer default 0",
    ],
    [   # v2: журнал действий и индексы
        "create table if not exists audit_log(id integer primary key, ts text default (datetime('now','localtime')), "
        "tg_id integer, action text not null, target text)",
        "create index if not exists ix_audit_ts on audit_log(ts)",
        "create index if not exists ix_lessons_student on lessons(student_id)",
        "create index if not exists ix_payments_student on payments(student_id)",
        "create index if not exists ix_regular_student on regular(student_id)",
    ],
]

def migrate(conn):
    """Применяет недостающие миграции по порядку; версия хранится в pragma user_version."""
    ver = conn.execute("pragma user_version").fetchone()[0]
    for i, stmts in enumerate(MIGRATIONS[ver:], start=ver + 1):
        for s in stmts:
            try:
                conn.execute(s)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise
        conn.execute(f"pragma user_version={i}")
        conn.commit()
        log.info("схема БД обновлена до версии %s", i)
    return len(MIGRATIONS)

def init_db(conn):
    conn.executescript(BASE_SCHEMA)
    migrate(conn)

init_db(db)
auth.init(db)     # таблицы авторизации (ссылки, сессии)
db.execute("delete from audit_log where ts < datetime('now','-365 days','localtime')"); db.commit()   # храним год

def _canon_days():
    """Старые записи с днём в нестандартном виде («20261005», «2026-W41-1») приводим к YYYY-MM-DD."""
    for tbl, col in (("lessons", "day"), ("regular", "start"), ("skips", "day")):
        for rid, d in db.execute(f"select rowid, {col} from {tbl} where {col} not glob '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'").fetchall():
            try:
                db.execute(f"update {tbl} set {col}=? where rowid=?", (date.fromisoformat(d).isoformat(), rid))
            except (ValueError, TypeError, sqlite3.IntegrityError):
                pass
    db.commit()
_canon_days()

def q(sql, *a): return db.execute(sql, a).fetchall()
def run(sql, *a):
    c = db.execute(sql, a); db.commit(); return c.lastrowid

AUDIT_KEYS = ("id", "student_id", "regular_id", "day", "time", "amount", "dur")   # только идентификаторы и суммы: без имён и фото

def audit(uid, action, target=""):
    try:
        db.execute("insert into audit_log(tg_id, action, target) values(?,?,?)", (uid, action, target[:200])); db.commit()
    except sqlite3.Error:
        log.exception("не удалось записать audit_log")

def money(n): return f"{n:,}".replace(",", " ") + f" {CUR}"
def hm(t): return f"{t[:2]}:{t[2:]}"

# ---------- логика занятий ----------
def mins(t): return int(t[:2]) * 60 + int(t[3:])

def conflict(day, t, dur, ex=0):
    """True, если занятие [t, t+dur) пересекается с другим занятием этого дня."""
    s, e = mins(t), mins(t) + dur
    return any(mins(o["time"]) < e and s < mins(o["time"]) + (o["dur"] or 60)
               for o in q("select id,time,dur from lessons where day=? and id!=?", day, ex))

def fill_regular():
    """Раскладывает регулярные занятия на ближайшие недели. Возвращает число пропущенных из-за занятого времени."""
    today = date.today()
    end = today + timedelta(weeks=WEEKS_AHEAD)
    skipped = 0
    for g in q("select * from regular"):
        d = max(date.fromisoformat(g["start"]), today)
        d += timedelta(days=(g["weekday"] - d.weekday()) % 7)
        while d <= end:
            ds = d.isoformat()
            if not q("select 1 from skips where regular_id=? and day=?", g["id"], ds) and \
               not q("select 1 from lessons where regular_id=? and day=?", g["id"], ds):
                dur = g["dur"] or 60
                if conflict(ds, g["time"], dur):
                    skipped += 1
                else:
                    db.execute("insert into lessons(student_id,day,time,regular_id,dur) values(?,?,?,?,?)",
                               (g["student_id"], ds, g["time"], g["id"], dur))
            d += timedelta(days=7)
    db.commit()
    return skipped

def make_regular(lid):
    l = q("select * from lessons where id=?", lid)[0]
    if l["regular_id"]:
        return 0
    rid = run("insert into regular(student_id,weekday,time,start,dur) values(?,?,?,?,?)",
              l["student_id"], date.fromisoformat(l["day"]).weekday(), l["time"], l["day"], l["dur"] or 60)
    run("update lessons set regular_id=? where id=?", rid, lid)
    return fill_regular()

def add_lesson(sid, day, t, regular=False, dur=60):
    """Возвращает (id, пропущено) или None, если время занято."""
    if conflict(day, t, dur):
        return None
    try:
        lid = run("insert into lessons(student_id,day,time,dur) values(?,?,?,?)", sid, day, t, dur)
    except sqlite3.IntegrityError:
        return None
    return lid, (make_regular(lid) if regular else 0)

def move_lesson(lid, day, t, dur=None):
    l = q("select * from lessons where id=?", lid)[0]
    dur = dur or l["dur"] or 60
    if conflict(day, t, dur, lid):
        return False
    try:
        db.execute("update lessons set day=?, time=?, dur=? where id=?", (day, t, dur, lid))
    except sqlite3.IntegrityError:
        return False
    if l["regular_id"] and l["day"] != day:      # старая дата серии не должна создаться заново
        db.execute("insert or ignore into skips values(?,?)", (l["regular_id"], l["day"]))
    db.commit()
    return True

def cancel_lesson(lid):
    l = q("select * from lessons where id=?", lid)[0]
    if l["regular_id"]:
        db.execute("insert or ignore into skips values(?,?)", (l["regular_id"], l["day"]))
    db.execute("delete from lessons where id=?", (lid,))
    db.commit()

def stop_series(lid):
    l = q("select * from lessons where id=?", lid)[0]
    rid = l["regular_id"]
    if not rid:
        return
    db.execute("delete from lessons where regular_id=? and day>=?", (rid, l["day"]))
    db.execute("update lessons set regular_id=null where regular_id=?", (rid,))
    db.execute("delete from regular where id=?", (rid,))
    db.execute("delete from skips where regular_id=?", (rid,))
    db.commit()

# ---------- баланс ----------
PAST = "datetime(l.day||' '||l.time) <= datetime('now','localtime')"

def stats():
    """done — проведённые занятия, spent — их стоимость: каждое по своей цене
    (замороженной при смене цены), а ещё не замороженные — по текущей цене ученика."""
    return q(f"""select s.id, s.name, s.price, coalesce(s.deleted,0) deleted,
      coalesce((select sum(amount) from payments where student_id=s.id),0) paid,
      (select count(*) from lessons l where l.student_id=s.id and {PAST}) done,
      (select coalesce(sum(coalesce(l.price, s.price)),0) from lessons l where l.student_id=s.id and {PAST}) spent
      from students s order by s.name""")

def set_price(sid, new):
    """Меняет цену занятия. Уже прошедшие занятия остаются по прежней цене (замораживаются),
    новые и будущие считаются по новой. Если раньше цена не была указана (0) — прошлое не замораживается."""
    new = max(0, int(new))
    old = q("select price from students where id=?", sid)[0]["price"] or 0
    if old == new: return
    if old:
        db.execute(f"update lessons set price=? where student_id=? and price is null and "
                   f"datetime(day||' '||time) <= datetime('now','localtime')", (old, sid))
    db.execute("update students set price=? where id=?", (new, sid))
    db.commit()

def balance_text(sid):
    s = next(x for x in stats() if x["id"] == sid)
    spent = s["spent"]
    bal = s["paid"] - spent
    lines = [f"Оплачено всего: {money(s['paid'])}"]
    if s["price"]:
        lines.append(f"Проведено занятий: {s['done']} на {money(spent)}")
        if bal < 0:
            status = f"🔴 Долг: {money(-bal)}"
        elif bal > 0:
            status = f"🟢 Оплачено наперёд: {money(bal)} (хватит на {bal // s['price']} зан.)"
        else:
            status = "✅ Долга нет, предоплаты нет"
    else:
        lines.append(f"Проведено занятий: {s['done']}")
        status = "ℹ️ Цена занятия не указана — долг не считается"
    return "\n".join(lines + ["", status])

def update_student(sid, name=None, photo=None):
    if name is not None:
        db.execute("update students set name=? where id=?", (name, sid))
    if photo is not None:
        db.execute("update students set photo=? where id=?", (photo or None, sid))
    db.commit()

def update_payment(pid, amount):
    db.execute("update payments set amount=? where id=?", (amount, pid))
    db.commit()

def delete_payment(pid):
    db.execute("delete from payments where id=?", (pid,))
    db.commit()

# ---------- оплаты по занятиям ----------
def lesson_pay_status(sid):
    """Занятия ученика по порядку; оплата закрывает занятия от старых к новым, каждое по своей цене.
    Возвращает (текущая цена, [(day, time, dur, paid, price)])."""
    st = q("select price from students where id=?", sid)[0]
    cur = st["price"] or 0
    paid_sum = q("select coalesce(sum(amount),0) s from payments where student_id=?", sid)[0]["s"]
    out = []
    for l in q("select day,time,coalesce(dur,60) dur, coalesce(price,?) lp from lessons where student_id=? order by day,time", cur, sid):
        lp = l["lp"] or 0
        ok = bool(lp) and paid_sum >= lp
        if ok: paid_sum -= lp
        out.append((l["day"], l["time"], l["dur"], ok, lp))
    return cur, out

def debts_prepaid(sid, today):
    """Долг — неоплаченные занятия раньше сегодня; предоплата — оплаченные позже сегодня.
    Возвращает (текущая цена, [(day, price)], [(day, price)])."""
    cur, ls = lesson_pay_status(sid)
    debts = sorted(((d, lp) for d, t, du, ok, lp in ls if d < today and not ok and lp), reverse=True)
    pre = sorted((d, lp) for d, t, du, ok, lp in ls if d > today and ok)
    return cur, debts, pre

from html import escape as esc

def num(n): return f"{n:,}".replace(",", " ")
def dmy(day): return date.fromisoformat(day).strftime("%d.%m.%Y")

def grade_text(g):
    g = (g or "").strip()
    return f"{g} класс" if g.isdigit() else g

def end_time(t, dur):
    e = mins(t) + dur
    return f"{e // 60 % 24:02d}:{e % 60:02d}"

def today_report():
    today = date.today()
    ds = today.isoformat()
    head = f"Сегодня:\n{WD_FULL[today.weekday()]} ({today:%d.%m.%Y})"
    ls = q("""select l.time, coalesce(l.dur,60) dur, s.id sid, s.name, s.grade, s.subject
              from lessons l join students s on s.id=l.student_id where l.day=? order by l.time""", ds)
    if not ls:
        return [head + "\n\nЗанятий сегодня нет."]
    blocks = []
    for i, l in enumerate(ls, 1):
        _, debts, pre = debts_prepaid(l["sid"], ds)
        _, st = lesson_pay_status(l["sid"])
        price, paid = next(((lp, ok) for d, t, du, ok, lp in st if d == ds and t == l["time"]), (0, False))
        rest = ". ".join(esc(x) for x in (grade_text(l["grade"]), (l["subject"] or "").strip()) if x)
        info = f"<b>{esc(l['name'])}</b>" + (f". {rest}" if rest else "")
        t = l["time"]
        lines = [f"<b>{i}. Урок: {t} - {end_time(t, l['dur'])} ({l['dur']} мин)</b>", info]
        if price:
            lines.append(f"{num(price)} за урок. " + ("Оплачено" if paid else "Не оплачено"))
        else:
            lines.append("Цена занятия не указана")
        if debts:
            lines.append("<i>" + "\n".join(["Долги:"] + [f"{num(p_)} за урок {dmy(d)}" for d, p_ in debts]) + "</i>")
        if pre:
            lines.append("<i>" + "\n".join(["Предоплата:"] + [f"{num(p_)} за урок {dmy(d)}" for d, p_ in pre]) + "</i>")
        if not debts and not pre:
            lines.append("<i>Предоплат и долгов нет.</i>")
        blocks.append("\n".join(lines))
    return chunks(head, blocks)

def chunks(head, blocks, limit=3800):
    msgs, cur = [], head
    for b in blocks:
        if len(cur) + len(b) + 2 > limit:
            msgs.append(cur); cur = b
        else:
            cur += "\n\n" + b
    msgs.append(cur)
    return msgs

def finance_report():
    today = date.today(); ds = today.isoformat()
    month = today.strftime("%Y-%m")
    got = q("select coalesce(sum(amount),0) s from payments where strftime('%Y-%m',created)=?", month)[0]["s"]
    held = 0; debt_rows = []; pre_total = 0
    for s in q("select id,name,price from students where coalesce(deleted,0)=0 order by name"):
        cur, debts, pre = debts_prepaid(s["id"], ds)
        held += q("select coalesce(sum(coalesce(price,?)),0) c from lessons where student_id=? and substr(day,1,7)=? and day<=?",
                  s["price"] or 0, s["id"], month, ds)[0]["c"]
        pre_total += sum(p_ for _, p_ in pre)
        if debts: debt_rows.append((s["name"], sum(p_ for _, p_ in debts), len(debts)))
    debt_total = sum(d[1] for d in debt_rows)
    lines = [f"Фин. отчет: {today:%m.%Y}", "",
             f"Получено оплат за месяц: {money(got)}",
             f"Проведено занятий на сумму: {money(held)}",
             f"Предоплачено вперёд: {money(pre_total)}",
             f"Общий долг: {money(debt_total)}"]
    if debt_rows:
        lines += ["", "Должники:"] + [f"{n} — {money(a)} ({c} зан.)" for n, a, c in sorted(debt_rows, key=lambda x: -x[1])]
    else:
        lines += ["", "Долгов нет."]
    return "\n".join(lines)

# ---------- PDF расписания ----------
import io, os
from reportlab.lib.colors import Color, black
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

def _font(name, fname):
    for p in (f"{HERE}/fonts/{fname}", f"/usr/share/fonts/truetype/dejavu/{fname}"):
        if os.path.exists(p):
            pdfmetrics.registerFont(TTFont(name, p))
            return
    raise RuntimeError(f"Не найден шрифт {fname}: положите его в папку fonts/")

_font("TS", "DejaVuSans.ttf")
_font("TSB", "DejaVuSans-Bold.ttf")

WDFULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

def _fit(text, font, size, width):
    """Обрезает строку с «…», чтобы она точно влезла в ширину."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    if stringWidth(text, font, size) <= width: return text
    while text and stringWidth(text + "…", font, size) > width: text = text[:-1]
    return text.rstrip() + "…"

def week_pdf(start):
    """A4 альбомная — копия «Предпросмотра печати» из мини-аппа (.bw): шапка дней с датами,
    слева часы 08–20 (линии часов идут только по колонкам дней, подписи их не пересекают),
    белые карточки занятий с жирной левой полосой, время, имя и класс в рамке."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    fill_regular()
    days = [start + timedelta(days=i) for i in range(7)]
    rows = q("""select l.day,l.time,coalesce(l.dur,60) dur,l.regular_id,s.name,s.grade,s.subject from lessons l
                join students s on s.id=l.student_id where l.day between ? and ? order by l.time""",
             days[0].isoformat(), days[-1].isoformat())

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=landscape(A4))
    W, H = landscape(A4)
    M, TC, HEAD = 14, 46, 33                 # поля, колонка часов, шапка дней
    # сетка всегда 08:00–21:00; расширяется только если занятие выходит за эти границы
    H0, H1 = 8, 21
    if rows:
        st_ = [int(x["time"][:2]) * 60 + int(x["time"][3:]) for x in rows]
        H0 = min(H0, max(0, min(st_) // 60))
        H1 = max(H1, min(24, -(-max(a + x["dur"] for a, x in zip(st_, rows)) // 60)))
    L, R = M, W - M
    x0 = L + TC
    colw = (R - x0) / 7

    ink, c555, c333, grey = Color(.07, .07, .07), Color(.33, .33, .33), Color(.2, .2, .2), Color(.27, .27, .27)
    c999, band, half = Color(.6, .6, .6), Color(.957, .957, .957), Color(.812, .812, .812)

    # заголовок: «Расписание на неделю» слева, даты справа, жирная линия под ним
    ty = H - M - 17
    c.setFillColor(ink); c.setFont("TSB", 19); c.drawString(L, ty, "Расписание на неделю")
    c.setFillColor(c333); c.setFont("TS", 11.5)
    c.drawRightString(R, ty, f"{days[0]:%d.%m.%Y} – {days[-1]:%d.%m.%Y}")
    c.setStrokeColor(ink); c.setLineWidth(1.6); c.line(L, ty - 7, R, ty - 7)

    gt, gb = ty - 7 - 8, M                   # верх и низ таблицы
    top0 = gt - HEAD                         # верх сетки часов
    n = H1 - H0
    rh = (top0 - gb) / n                     # высота одного часа

    # фон: чередование часов, получасовые и часовые линии — только по колонкам дней
    for j in range(n):
        y = top0 - j * rh
        if j % 2 == 1:
            c.setFillColor(band); c.rect(x0, y - rh, R - x0, rh, fill=1, stroke=0)
        c.setStrokeColor(half); c.setLineWidth(.5); c.line(x0, y - rh / 2, R, y - rh / 2)
        c.setStrokeColor(c555); c.setLineWidth(.6); c.line(x0, y - rh, R, y - rh)

    # шапка дней
    for i, d in enumerate(days):
        cx = x0 + i * colw + colw / 2
        c.setFillColor(ink); c.setFont("TSB", 11); c.drawCentredString(cx, gt - 13.5, WDFULL[i])
        c.setFillColor(grey); c.setFont("TS", 9.5); c.drawCentredString(cx, gt - 26, f"{d:%d.%m.%Y}")
    c.setStrokeColor(ink); c.setLineWidth(1.6); c.line(L, top0, R, top0)
    c.setStrokeColor(c555); c.setLineWidth(.6)
    for i in range(7):
        x = x0 + i * colw; c.line(x, gt, x, gb)

    # подписи часов — по центру линии часа, первая прижата к верху
    c.setFillColor(ink); c.setFont("TSB", 10)
    for j in range(n):
        y = top0 - j * rh
        c.drawRightString(x0 - 6, (y - 11) if j == 0 else (y - 3.5), f"{H0 + j:02d}:00")

    # Карточка = две строки: 1) время жирным + (длительность)  2) «Фамилия Имя 9 кл.» жирным.
    # Кегль — максимальный, при котором обе строки влезают по ширине, а в самой короткой карточке (45 мин)
    # остаются одинаковые красивые отступы сверху и снизу.
    minh = 45 / 60 * rh - .8                  # высота самой короткой карточки (45 мин)
    CAP, DESC, PITCH = .73, .24, 1.3          # высота заглавных, выносные элементы, расстояние между строками (в кеглях)
    SW = 1.8                                  # ширина левой полосы
    PAD_X, PAD_R = 4.0, 3.0
    twid = colw - 4.6 - SW - PAD_X - PAD_R    # ширина текста в карточке
    DS = .82                                  # длительность в скобках чуть мельче времени

    def tline_w(t, f, dur_txt):
        return stringWidth(t, "TSB", f) + stringWidth(" " + dur_txt, "TS", f * DS)

    f = 12.0
    while f > 6 and tline_w("00:00–00:00", f, "(00 мин)") > twid: f -= .05
    f = min(f, (minh - 8) / (CAP + PITCH + DESC))        # минимум по 4 pt сверху и снизу в карточке на 45 мин
    block = f * (CAP + PITCH + DESC)
    GT = (minh - block) / 2                    # отступ сверху (= снизу в карточке на 45 мин)
    b1 = -(GT + CAP * f); b2 = b1 - PITCH * f

    def name_line(name, cls, f, width):
        """«Имя Фамилия» + класс в одной строке (в базе имя хранится как «Имя Фамилия»).
        Если не влезает — имя целиком, а фамилия сокращается до первой буквы с точкой."""
        parts = name.split()
        first, last = parts[0], " ".join(parts[1:])
        cands = [name]
        if last: cands.append(first + " " + last[0] + ".")
        cw_ = stringWidth(" " + cls, "TSB", f) if cls else 0
        for t in cands:
            if stringWidth(t, "TSB", f) + cw_ <= width: return t, cls
        return cands[-1], cls

    # карточки занятий
    for x in rows:
        i = (date.fromisoformat(x["day"]) - days[0]).days
        s = int(x["time"][:2]) * 60 + int(x["time"][3:]); e = s + x["dur"]
        bx = x0 + i * colw + 2.6; bwid = colw - 4.6
        yt = top0 - (s - H0 * 60) / 60 * rh
        hgt = x["dur"] / 60 * rh - .8
        ytop, ybot = min(yt, top0), max(yt - hgt, top0 - n * rh)
        if ybot >= ytop: continue
        h = ytop - ybot
        g = (x["grade"] or "").strip()
        cls = f"{g} кл." if g.isdigit() else g
        nm = " ".join(x["name"].split())
        end = f"{e // 60:02d}:{e % 60:02d}"
        trange = f"{x['time']}–{end}"
        # регулярные занятия помечены «повтор» (как в предпросмотре печати мини-аппа)
        dur_txt = f"({x['dur']} мин · повтор)" if x["regular_id"] else f"({x['dur']} мин)"

        c.saveState()
        p = c.beginPath(); p.roundRect(bx, ybot, bwid, h, 2.2)
        c.clipPath(p, stroke=0, fill=0)
        c.setFillColor(Color(1, 1, 1)); c.rect(bx, ybot, bwid, h, fill=1, stroke=0)
        c.setFillColor(Color(.35, .35, .35)); c.rect(bx, ybot, SW, h, fill=1, stroke=0)   # узкая тёмно-серая полоса слева
        tx = bx + SW + PAD_X; tw = bwid - SW - PAD_X - PAD_R

        # строка 1: время жирным + длительность
        fs = min(f, f * tw / tline_w(trange, f, dur_txt))
        c.setFillColor(ink); c.setFont("TSB", fs); c.drawString(tx, ytop + b1, trange)
        c.setFillColor(c333); c.setFont("TS", fs * DS)
        c.drawString(tx + stringWidth(trange, "TSB", fs) + stringWidth(" ", "TS", fs * DS), ytop + b1, dur_txt)
        # строка 2: имя + класс
        t2, k2 = name_line(nm, cls, f, tw)
        full2 = t2 + (" " + k2 if k2 else "")
        f2 = max(min(f, f * tw / stringWidth(full2, "TSB", f)), f * .8)   # совсем длинная строка чуть мельче, имя не режем
        if stringWidth(full2, "TSB", f2) > tw:                               # крайний случай: даже так не влезает
            t2 = _fit(t2, "TSB", f2, tw - (stringWidth(" " + k2, "TSB", f2) if k2 else 0))
        c.setFillColor(ink); c.setFont("TSB", f2); c.drawString(tx, ytop + b2, t2)
        if k2: c.drawString(tx + stringWidth(t2 + " ", "TSB", f2), ytop + b2, k2)
        c.restoreState()
        c.setStrokeColor(c333); c.setLineWidth(.6); c.roundRect(bx, ybot, bwid, h, 2.2, fill=0, stroke=1)

    # внешняя рамка таблицы
    c.setStrokeColor(c999); c.setLineWidth(.8); c.rect(L, gb, R - L, gt - gb, fill=0, stroke=1)
    c.save()
    return buf.getvalue()

# ---------- клавиатуры ----------
def menu_kb():
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text="Занятия сегодня"), KeyboardButton(text="Новое занятие"))
    b.row(KeyboardButton(text="Новая оплата"), KeyboardButton(text="Фин. отчет"))
    b.row(KeyboardButton(text="Расписание"), KeyboardButton(text="Новый ученик"))
    return b.as_markup(resize_keyboard=True)

def pager(b, prefix, page, pages, label=None):
    """Ряд навигации: ◀️  метка  ▶️ (кнопки только там, где есть куда листать)."""
    row = []
    if page > 0: row.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:{page-1}"))
    row.append(InlineKeyboardButton(text=label or f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1: row.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:{page+1}"))
    b.row(*row)

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

def dates_kb(off):
    """Календарь месяца: сверху месяц со стрелками, затем дни недели и сетка дат (прошедшие дни неактивны)."""
    import calendar
    today = date.today()
    y, mo = divmod(today.year * 12 + today.month - 1 + off, 12)
    y, mo = y, mo + 1
    b = InlineKeyboardBuilder()
    nav = []
    if off > 0: nav.append(InlineKeyboardButton(text="◀️", callback_data=f"ld:{off-1}"))
    nav.append(InlineKeyboardButton(text=f"{MONTHS[mo-1]} {y}", callback_data="noop"))
    nav.append(InlineKeyboardButton(text="▶️", callback_data=f"ld:{off+1}"))
    b.row(*nav)
    b.row(*[InlineKeyboardButton(text=w, callback_data="noop") for w in WD])
    for week in calendar.Calendar(0).monthdatescalendar(y, mo):
        row = []
        for d in week:
            if d.month != mo or d < today:
                row.append(InlineKeyboardButton(text="·" if d.month == mo else "\u2800", callback_data="noop"))
            else:
                mark = f"[{d.day}]" if d == today else str(d.day)
                row.append(InlineKeyboardButton(text=mark, callback_data=f"lt:{d.isoformat()}:0"))
        b.row(*row)
    if off == 0:
        tm = today + timedelta(days=1)
        b.row(InlineKeyboardButton(text=f"Сегодня, {WD[today.weekday()]} {today:%d.%m}", callback_data=f"lt:{today.isoformat()}:0"),
              InlineKeyboardButton(text=f"Завтра, {WD[tm.weekday()]} {tm:%d.%m}", callback_data=f"lt:{tm.isoformat()}:0"))
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data="lb"))      # к списку учеников
    return b.as_markup()

SLOTS = [f"{m // 60:02d}:{m % 60:02d}" for m in range(8 * 60, 21 * 60 + 1, 30)]   # 08:00 … 21:00 через 30 мин
SLOTS_PER = 12
DURS = list(range(45, 181, 15))                                                       # 45 … 180 мин (минимум 45)
DURS_PER = 6

def time_kb(day, page=0):
    """Занятые слоты: точное совпадение ИЛИ пересечение с другим занятием
    (пересечения запрещены — такие слоты сразу помечаются 🔒)."""
    rows = q("select time,dur from lessons where day=?", day)
    busy = {t for t in SLOTS
            if any(mins(t) < mins(o["time"]) + (o["dur"] or 60) and mins(o["time"]) < mins(t) + DURS[0]
                   for o in rows)}
    pages = -(-len(SLOTS) // SLOTS_PER)
    page = max(0, min(page, pages - 1))
    b = InlineKeyboardBuilder()
    for t in SLOTS[page * SLOTS_PER:(page + 1) * SLOTS_PER]:
        b.button(text=("🔒 " if t in busy else "") + t,
                 callback_data="busy" if t in busy else f"lm:{day}:{t[:2]}{t[3:]}")
    b.adjust(3)
    pager(b, f"lt:{day}", page, pages)
    b.row(InlineKeyboardButton(text="🕐 Своё время (например 8:25)", callback_data=f"lo:{day}"))
    b.row(InlineKeyboardButton(text="◀️ К датам", callback_data="ld:0"))
    return b.as_markup()

def mode_kb(day, t):
    b = InlineKeyboardBuilder()
    b.button(text="Разовое занятие", callback_data=f"lc:{day}:{t}:o")
    b.button(text="🔁 Сделать регулярным", callback_data=f"lc:{day}:{t}:r")
    b.button(text="◀️ К длительности", callback_data=f"lm:{day}:{t}")
    b.adjust(1)
    return b.as_markup()

def dur_kb(day, t, page=0):
    pages = -(-len(DURS) // DURS_PER)
    page = max(0, min(page, pages - 1))
    b = InlineKeyboardBuilder()
    for d in DURS[page * DURS_PER:(page + 1) * DURS_PER]:
        b.button(text=f"{d} мин", callback_data=f"lq:{day}:{t}:{d}")
    b.adjust(3)
    pager(b, f"lw:{day}:{t}", page, pages)
    b.row(InlineKeyboardButton(text="◀️ К времени", callback_data=f"lt:{day}:0"))
    return b.as_markup()

def students_kb(pick, more, page, new_cb):
    rows = q("select id,name from students where coalesce(deleted,0)=0 order by name")
    b = InlineKeyboardBuilder()
    for r_ in rows[page * PER:(page + 1) * PER]:
        b.button(text=r_["name"], callback_data=f"{pick}:{r_['id']}")
    b.adjust(1)
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="◀️", callback_data=f"{more}:{page-1}"))
    if (page + 1) * PER < len(rows): nav.append(InlineKeyboardButton(text="▶️", callback_data=f"{more}:{page+1}"))
    if nav: b.row(*nav)
    b.row(InlineKeyboardButton(text="➕ Добавить нового ученика", callback_data=new_cb))
    return b.as_markup()

def amount_kb(sid):
    b = InlineKeyboardBuilder()
    for a in (3000, 5000, 10000):
        b.button(text=f"{a:,}".replace(",", " "), callback_data=f"pm:{sid}:{a}")
    b.adjust(3)
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data="pb"))      # к списку учеников
    return b.as_markup()

GRADES = [str(i) for i in range(1, 12)]     # 1–11 классы
GRADES_PER = 6
PRICES = (3000, 4000, 5000)

def step_nav(b, back, skip=None):
    """Нижний ряд шага: «Назад» и (если шаг необязательный) «Пропустить»."""
    row = [InlineKeyboardButton(text="◀️ Назад", callback_data=f"nsb:{back}")]
    if skip: row.append(InlineKeyboardButton(text="Пропустить ⏭", callback_data=f"nsk:{skip}"))
    b.row(*row)

def grade_kb(page=0):
    pages = -(-len(GRADES) // GRADES_PER)
    page = max(0, min(page, pages - 1))
    b = InlineKeyboardBuilder()
    for g in GRADES[page * GRADES_PER:(page + 1) * GRADES_PER]:
        b.button(text=f"{g} класс", callback_data=f"nsc:{g}")
    b.adjust(3)
    pager(b, "nsg", page, pages)
    step_nav(b, "grade", "grade")
    return b.as_markup()

def price_kb():
    b = InlineKeyboardBuilder()
    for a in PRICES:
        b.button(text=num(a), callback_data=f"nsp:{a}")
    b.button(text="✏️ Свой вариант", callback_data="nsx")
    b.adjust(3, 1)
    step_nav(b, "price", "price")
    return b.as_markup()

def single_nav_kb(back, skip=None):
    b = InlineKeyboardBuilder()
    step_nav(b, back, skip)
    return b.as_markup()

# ---------- состояния ----------
class NewStudent(StatesGroup):
    name = State()
    grade = State()
    subject = State()
    price = State()
    price_custom = State()

class CustomTime(StatesGroup):
    day = State()

class Pay(StatesGroup):
    amount = State()

def _allowed(e):
    u = e.from_user
    return bool(u) and auth.allowed(u.id, u.username)

async def _allowed_f(e):          # async: фильтр выполняется в потоке event loop, а не в пуле (общее соединение SQLite)
    return _allowed(e)

r = Router()
r.message.filter(_allowed_f)
r.callback_query.filter(_allowed_f)

# Публичный роутер: ТОЛЬКО /start (выдача ссылки на вход). Всё остальное — в роутере r за проверкой входа.
pub = Router()
pub.message.filter(_allowed_f)

class AuthMW(BaseMiddleware):
    """Deny by default: в роутер r попадают только вошедшие, исключений нет."""
    async def __call__(self, handler, event, data):
        uid = event.from_user.id if event.from_user else 0
        if not _allowed(event):
            return                                               # чужим бот не отвечает вовсе
        if auth.is_authorized(uid):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Нужен вход — нажмите /start", show_alert=True)
        else:
            await event.answer("Сначала войдите — нажмите /start", reply_markup=ReplyKeyboardRemove())

r.message.outer_middleware(AuthMW())
r.callback_query.outer_middleware(AuthMW())

def pretty(day, t):
    d = date.fromisoformat(day)
    return f"{WD[d.weekday()]} {d:%d.%m} в {t}"

# ---------- общие шаги ----------
async def make_lesson(m: Message, day, t, sid, edit, regular, dur=60):
    name = q("select name from students where id=?", sid)[0]["name"]
    res = add_lesson(sid, day, t, regular, dur)
    if res is None:
        text = "⚠️ Время занято или пересекается с другим занятием. Выберите другое."
    else:
        text = f"✅ Новое занятие добавлено\n👤 {name}\n📅 {pretty(day, t)}, {dur} мин"
        if regular:
            wd = WD[date.fromisoformat(day).weekday()]
            text += f"\n🔁 Регулярно каждую неделю: {wd} в {t}"
            if res[1]:
                text += f"\n⚠️ Не поставлено из-за занятого времени: {res[1]}"
    await (m.edit_text if edit else m.answer)(text)

async def ask_amount(m: Message, state: FSMContext, sid, edit):
    name = q("select name from students where id=?", sid)[0]["name"]
    await state.set_state(Pay.amount)
    await state.update_data(sid=sid)
    await (m.edit_text if edit else m.answer)(
        f"{name}\nВведите сумму или выберите кнопкой:", reply_markup=amount_kb(sid))

def student_card(name, grade, subject, price):
    return "\n".join([
        "✅ Новый ученик успешно добавлен", "",
        f"👤 Имя: {name}",
        f"🎓 Класс: {grade_text(grade) or '—'}",
        f"📚 Занятия: {subject or '—'}",
        f"💰 Цена за занятие: {money(price) if price else 'не указана'}"])

async def student_done(m: Message, state: FSMContext, name, price, grade="", subject=""):
    then = (await state.get_data()).get("then")
    sid = run("insert into students(name,price,grade,subject) values(?,?,?,?)", name, price, grade or None, subject or None)
    await state.clear()
    await m.answer(student_card(name, grade, subject, price))
    if then == "lesson":
        await state.update_data(sid=sid)
        await m.answer("Выберите дату:", reply_markup=dates_kb(0))
    elif then == "pay":
        await ask_amount(m, state, sid, False)
    elif then and then.startswith("l:"):
        _, day, t, mode = then.split(":")
        await make_lesson(m, day, hm(t), sid, False, mode == "r")

# ---------- меню ----------
@pub.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    if auth.is_authorized(m.from_user.id):
        await m.answer("Готово к работе. Выберите действие в меню.", reply_markup=menu_kb())
        return
    await m.answer("🔐 Для работы с ботом и кабинетом нужно войти.", reply_markup=ReplyKeyboardRemove())
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Войти", url=auth.new_link(m.from_user.id, m.from_user.username or ""))]])
    await m.answer("Нажмите кнопку и введите логин и пароль. Ссылка одноразовая, действует 10 минут.", reply_markup=kb)

@r.message(Command("logout"))
async def logout(m: Message, state: FSMContext):
    await state.clear()
    auth.logout(m.from_user.id)
    await m.answer("Вы вышли. Чтобы войти снова, нажмите /start.", reply_markup=ReplyKeyboardRemove())

@r.message(F.text == "Занятия сегодня")
async def today_lessons(m: Message, state: FSMContext):
    await state.clear()
    for part in today_report():
        await m.answer(part, parse_mode="HTML")

@r.message(F.text == "Фин. отчет")
async def fin_report(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(finance_report())

def weeks_kb():
    """4 кнопки друг под другом: текущая неделя и три следующие (на кнопках — даты недель)."""
    mon0 = date.today() - timedelta(days=date.today().weekday())
    b = InlineKeyboardBuilder()
    for i in range(4):
        s_ = mon0 + timedelta(weeks=i)
        label = f"{s_:%d.%m} – {s_ + timedelta(days=6):%d.%m}"
        b.button(text=("Текущая: " + label) if i == 0 else label, callback_data=f"wk:{s_.isoformat()}")
    b.adjust(1)
    return b.as_markup()

def week_doc(start):
    """PDF недели как документ для отправки в Telegram + подпись (бот в чате и кнопка «Поделиться PDF» в мини-аппе)."""
    return (BufferedInputFile(week_pdf(start), filename=f"raspisanie_{start}.pdf"),
            f"📅 Расписание на неделю {start:%d.%m} – {start + timedelta(days=6):%d.%m.%Y}\nФормат A4, готово к печати.")

@r.message(F.text == "Расписание")
async def week_schedule(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Выберите неделю:", reply_markup=weeks_kb())

@r.callback_query(F.data.startswith("wk:"))
async def c_week(c: CallbackQuery):
    start = monday(c.data[3:])
    await c.answer()
    doc, caption = week_doc(start)
    await c.message.answer_document(doc, caption=caption)

@r.message(F.text == "Новое занятие")
async def new_lesson(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Кого записываем?", reply_markup=students_kb("lp", "lpp", 0, "lpn"))

@r.callback_query(F.data.startswith("lpp:"))
async def c_lp_page(c: CallbackQuery):
    await c.message.edit_text("Кого записываем?", reply_markup=students_kb("lp", "lpp", int(c.data[4:]), "lpn"))

@r.callback_query(F.data.startswith("lp:"))
async def c_lp(c: CallbackQuery, state: FSMContext):
    await state.update_data(sid=int(c.data[3:]))
    await c.message.edit_text("Выберите дату:", reply_markup=dates_kb(0))

@r.callback_query(F.data == "lpn")
async def c_lp_new(c: CallbackQuery, state: FSMContext):
    await state.update_data(then="lesson")
    await ns_show(c.message, state, "name")
    await c.answer()

@r.callback_query(F.data.startswith("lq:"))
async def c_dur(c: CallbackQuery, state: FSMContext):
    _, day, t, d = c.data.split(":")
    await state.update_data(dur=int(d))
    await c.message.edit_text(f"{pretty(day, hm(t))}, {d} мин\nКакое занятие?", reply_markup=mode_kb(day, t))

@r.message(F.text == "Новая оплата")
async def new_pay(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Выберите ученика:", reply_markup=students_kb("pa", "pu", 0, "pn"))

@r.message(F.text == "Новый ученик")
async def new_student(m: Message, state: FSMContext):
    await state.clear()
    await ns_show(m, state, "name")

# ---------- занятие ----------
@r.callback_query(F.data.startswith("ld:"))
async def c_dates(c: CallbackQuery):
    await c.message.edit_text("Выберите дату:", reply_markup=dates_kb(int(c.data[3:])))

@r.callback_query(F.data == "noop")
async def c_noop(c: CallbackQuery):
    await c.answer()

@r.callback_query(F.data == "lb")
async def c_back_students(c: CallbackQuery):
    await c.message.edit_text("Кого записываем?", reply_markup=students_kb("lp", "lpp", 0, "lpn"))

@r.callback_query(F.data == "pb")
async def c_back_pay_students(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Выберите ученика:", reply_markup=students_kb("pa", "pu", 0, "pn"))

@r.callback_query(F.data.startswith("lt:"))
async def c_times(c: CallbackQuery, state: FSMContext):
    await state.set_state(None)       # выход из ввода своего времени (данные шага сохраняются)
    fill_regular()
    parts = c.data.split(":")
    day, page = parts[1], int(parts[2]) if len(parts) > 2 else 0
    d = date.fromisoformat(day)
    await c.message.edit_text(f"{WD[d.weekday()]} {d:%d.%m} — выберите время:", reply_markup=time_kb(day, page))

@r.callback_query(F.data.startswith("lw:"))
async def c_dur_page(c: CallbackQuery):
    _, day, t, page = c.data.split(":")
    await c.message.edit_text(f"{pretty(day, hm(t))}\nДлительность занятия?", reply_markup=dur_kb(day, t, int(page)))

@r.callback_query(F.data == "busy")
async def c_busy(c: CallbackQuery):
    await c.answer("Это время занято", show_alert=True)

@r.callback_query(F.data.startswith("lm:"))
async def c_mode(c: CallbackQuery):
    _, day, t = c.data.split(":")
    await c.message.edit_text(f"{pretty(day, hm(t))}\nДлительность занятия?", reply_markup=dur_kb(day, t))

TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.\s]([0-5]\d)$")

@r.callback_query(F.data.startswith("lo:"))
async def c_time_custom(c: CallbackQuery, state: FSMContext):
    day = c.data[3:]
    await state.set_state(CustomTime.day)
    await state.update_data(day=day)
    d = date.fromisoformat(day)
    await c.message.answer(f"{WD[d.weekday()]} {d:%d.%m} — введите время в формате ЧЧ:ММ, например 8:25",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=f"lt:{day}:0")]]))
    await c.answer()

@r.message(CustomTime.day, F.text.regexp(TIME_RE))
async def m_time_custom(m: Message, state: FSMContext):
    day = (await state.get_data())["day"]
    hh, mm = TIME_RE.match(m.text.strip()).groups()
    t = f"{int(hh):02d}{mm}"
    await state.set_state(None)
    await m.answer(f"{pretty(day, hm(t))}\nДлительность занятия?", reply_markup=dur_kb(day, t))

@r.message(CustomTime.day)
async def m_time_custom_bad(m: Message):
    await m.answer("Не получилось распознать время. Введите в формате ЧЧ:ММ, например 8:25 или 14:30")

@r.callback_query(F.data.startswith("lu:"))
async def c_lesson_students(c: CallbackQuery):
    _, day, t, mode, page = c.data.split(":")
    title = pretty(day, hm(t)) + (" · 🔁 регулярное" if mode == "r" else " · разовое")
    await c.message.edit_text(
        f"{title}\nКого записать?",
        reply_markup=students_kb(f"lc:{day}:{t}:{mode}", f"lu:{day}:{t}:{mode}", int(page), f"ln:{day}:{t}:{mode}"))

@r.callback_query(F.data.startswith("lc:"))
async def c_lesson_create(c: CallbackQuery, state: FSMContext):
    _, day, t, mode = c.data.split(":")[:4]
    d = await state.get_data()
    await make_lesson(c.message, day, hm(t), d["sid"], True, mode == "r", d.get("dur", 60))
    await state.clear()

@r.callback_query(F.data.startswith("ln:"))
async def c_lesson_new_student(c: CallbackQuery, state: FSMContext):
    _, day, t, mode = c.data.split(":")
    await state.update_data(then=f"l:{day}:{t}:{mode}")
    await ns_show(c.message, state, "name")
    await c.answer()

# ---------- оплата ----------
@r.callback_query(F.data.startswith("pu:"))
async def c_pay_students(c: CallbackQuery):
    await c.message.edit_text("Выберите ученика:",
                              reply_markup=students_kb("pa", "pu", int(c.data[3:]), "pn"))

@r.callback_query(F.data == "pn")
async def c_pay_new_student(c: CallbackQuery, state: FSMContext):
    await state.update_data(then="pay")
    await ns_show(c.message, state, "name")
    await c.answer()

@r.callback_query(F.data.startswith("pa:"))
async def c_pay_amount(c: CallbackQuery, state: FSMContext):
    await ask_amount(c.message, state, int(c.data[3:]), True)

def pay_alloc(sid, pid):
    """Куда пошла оплата pid: оплаты ученика закрывают его занятия от старых к новым (каждое по своей цене).
    Возвращает ([(день, цена урока, оплачено из цены)], остаток без занятия)."""
    cur = q("select price from students where id=?", sid)[0]["price"] or 0
    ls = [{"day": l["day"], "price": l["lp"], "rem": l["lp"]}
          for l in q("select day,time,coalesce(price,?) lp from lessons where student_id=? order by day,time", cur, sid) if l["lp"]]
    for p in q("select id,amount from payments where student_id=? order by created,id", sid):
        left, got = p["amount"], []
        for x in ls:
            if left <= 0: break
            if x["rem"] <= 0: continue
            t = min(left, x["rem"]); x["rem"] -= t; left -= t
            got.append((x["day"], x["price"], x["price"] - x["rem"]))
        if p["id"] == pid:
            return got, left
    return [], 0

def pay_report(sid, pid, amount):
    s = next(x for x in stats() if x["id"] == sid)
    got, left = pay_alloc(sid, pid)
    today = date.today().isoformat()
    groups = {"Долги": [], "Сегодня": [], "Предоплаты": []}
    for day, price, cov in got:
        key = "Долги" if day < today else "Сегодня" if day == today else "Предоплаты"
        tail = money(price) if cov >= price else \
            f"{'Частичная предоплата' if key == 'Предоплаты' else 'Частичная оплата'}: {money(cov)} из {money(price)}"
        groups[key].append(f"{dmy(day)} — {tail}")
    out = ["✅ <b>Пополнение прошло успешно</b>", f"+{money(amount)} · {esc(s['name'])}", "", "На что распределилась сумма:"]
    for k, items in groups.items():
        if items: out += ["", f"<b>{k}</b>"] + items
    if left > 0:
        out += ["", "<b>Остаток без занятия</b>", f"{money(left)} — останется предоплатой"]
    bal = s["paid"] - s["spent"]
    sign = "+" if bal > 0 else "−" if bal < 0 else ""
    out += ["", f"Баланс: {sign}{money(abs(bal))}",
            "Есть долги" if bal < 0 else "Есть предоплаты" if bal > 0 else "Все оплачено, долгов нет"]
    return "\n".join(out)

async def record_payment(m: Message, state: FSMContext, sid, amount, edit):
    pid = run("insert into payments(student_id,amount) values(?,?)", sid, amount)
    await state.clear()
    await (m.edit_text if edit else m.answer)(pay_report(sid, pid, amount), parse_mode="HTML")

@r.callback_query(F.data.startswith("pm:"))
async def c_pay_quick(c: CallbackQuery, state: FSMContext):
    _, sid, amount = c.data.split(":")
    await record_payment(c.message, state, int(sid), int(amount), True)

@r.message(Pay.amount, F.text.regexp(r"^\d[\d\s]*$"))
async def m_pay_manual(m: Message, state: FSMContext):
    sid = (await state.get_data())["sid"]
    await record_payment(m, state, sid, int(m.text.replace(" ", "")), False)

@r.message(Pay.amount)
async def m_pay_bad(m: Message):
    await m.answer("Введите сумму цифрами, например 4500")

# ---------- новый ученик (шаги: имя → класс → занятия → цена) ----------
async def ns_show(m: Message, state: FSMContext, step, edit=False, page=0):
    """Показывает шаг мастера. edit=True — правим текущее сообщение (кнопки),
    иначе шлём новое и убираем кнопки у предыдущего вопроса."""
    d = await state.get_data()
    if step == "name":
        await state.set_state(NewStudent.name)
        text, kb = "Шаг 1/4. Как зовут ученика?", single_nav_kb("name")
    elif step == "grade":
        await state.set_state(NewStudent.grade)
        text, kb = f"Шаг 2/4. {d['name']}\nВыберите класс:", grade_kb(page)
    elif step == "subject":
        await state.set_state(NewStudent.subject)
        text = "Шаг 3/4. Название занятий (необязательно)\nНапример: ЕГЭ по русскому"
        kb = single_nav_kb("subject", "subject")
    elif step == "price":
        await state.set_state(NewStudent.price)
        text, kb = "Шаг 4/4. Цена за занятие:", price_kb()
    else:   # price_custom
        await state.set_state(NewStudent.price_custom)
        text, kb = "Введите цену за занятие цифрами, например 3500", single_nav_kb("price_custom")
    if edit:
        await m.edit_text(text, reply_markup=kb)
        return
    if d.get("prompt"):
        try: await m.bot.edit_message_reply_markup(m.chat.id, d["prompt"])
        except Exception: pass
    sent = await m.answer(text, reply_markup=kb)
    await state.update_data(prompt=sent.message_id)

async def ns_finish(m: Message, state: FSMContext, price):
    d = await state.get_data()
    try: await m.edit_reply_markup()
    except Exception: pass
    await student_done(m, state, d["name"], price, d.get("grade"), d.get("subject"))

async def ns_alive(c: CallbackQuery, state: FSMContext):
    cur = await state.get_state()
    if not cur or not cur.startswith("NewStudent:"):
        await c.answer("Сессия устарела, нажмите «Новый ученик» заново", show_alert=True)
        return False
    return True

NS_BACK = {"grade": "name", "subject": "grade", "price": "subject", "price_custom": "price"}
NS_SKIP = {"grade": "subject", "subject": "price"}

@r.callback_query(F.data.startswith("nsb:"))
async def c_ns_back(c: CallbackQuery, state: FSMContext):
    if not await ns_alive(c, state): return
    step = c.data[4:]
    if step == "name":                     # с первого шага — выход из мастера
        then = (await state.get_data()).get("then")
        await state.clear()
        if then == "lesson":
            await c.message.edit_text("Кого записываем?", reply_markup=students_kb("lp", "lpp", 0, "lpn"))
        elif then == "pay":
            await c.message.edit_text("Выберите ученика:", reply_markup=students_kb("pa", "pu", 0, "pn"))
        else:
            await c.message.edit_text("Добавление ученика отменено.")
        return await c.answer()
    await ns_show(c.message, state, NS_BACK[step], edit=True)
    await c.answer()

@r.callback_query(F.data.startswith("nsk:"))
async def c_ns_skip(c: CallbackQuery, state: FSMContext):
    if not await ns_alive(c, state): return
    step = c.data[4:]
    if step == "price":
        await ns_finish(c.message, state, 0)
    else:
        await state.update_data(**{step: ""})
        await ns_show(c.message, state, NS_SKIP[step], edit=True)
    await c.answer()

@r.callback_query(F.data.startswith("nsg:"))
async def c_ns_grade_page(c: CallbackQuery, state: FSMContext):
    if not await ns_alive(c, state): return
    await ns_show(c.message, state, "grade", edit=True, page=int(c.data[4:]))
    await c.answer()

@r.callback_query(F.data.startswith("nsc:"))
async def c_ns_grade(c: CallbackQuery, state: FSMContext):
    if not await ns_alive(c, state): return
    await state.update_data(grade=c.data[4:])
    await ns_show(c.message, state, "subject", edit=True)
    await c.answer()

@r.callback_query(F.data.startswith("nsp:"))
async def c_ns_price(c: CallbackQuery, state: FSMContext):
    if not await ns_alive(c, state): return
    await ns_finish(c.message, state, int(c.data[4:]))
    await c.answer()

@r.callback_query(F.data == "nsx")
async def c_ns_price_custom(c: CallbackQuery, state: FSMContext):
    if not await ns_alive(c, state): return
    await ns_show(c.message, state, "price_custom", edit=True)
    await c.answer()

@r.message(NewStudent.name, F.text)
async def m_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await ns_show(m, state, "grade")

@r.message(NewStudent.grade, F.text)
async def m_grade(m: Message):                             # класс выбирается только кнопками
    await m.answer("Выберите класс кнопками выше или нажмите «Пропустить».")

@r.message(NewStudent.subject, F.text)
async def m_subject(m: Message, state: FSMContext):
    await state.update_data(subject=m.text.strip())
    await ns_show(m, state, "price")

@r.message(NewStudent.price, F.text.regexp(r"^\d[\d\s]*$"))
async def m_price(m: Message, state: FSMContext):          # цену можно и просто написать
    await ns_finish(m, state, int(m.text.replace(" ", "")))

@r.message(NewStudent.price_custom, F.text.regexp(r"^\d[\d\s]*$"))
async def m_price_custom(m: Message, state: FSMContext):
    await ns_finish(m, state, int(m.text.replace(" ", "")))

@r.message(NewStudent.price)
@r.message(NewStudent.price_custom)
async def m_price_bad(m: Message):
    await m.answer("Введите цену цифрами, например 3500")

# ---------- API для мини-аппа ----------
INIT_MAX_AGE = 24 * 3600     # initData старше суток не принимаем: украденную строку нельзя воспроизводить вечно

PDF_TTL = 600   # ссылка на PDF живёт 10 минут: хватает на «Сохранить», но не для повторного использования

def pdf_token(uid, start):
    """Подписанная ссылка на PDF. Нужна для Telegram.WebApp.downloadFile и DownloadManager в APK:
    нативные загрузчики идут по URL без заголовков X-Init/X-Session, поэтому у ручки PDF
    должен быть отдельный пропуск — привязан к пользователю и неделе, короткоживущий."""
    exp = int(time.time()) + PDF_TTL
    sig = hmac.new(TOKEN.encode(), f"pdf:{uid}:{start}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{uid}.{sig}"

def check_pdf_token(raw, start):
    """tg_id — ссылка валидна; иначе None. Подпись сверяется за постоянное время."""
    try:
        exp_s, uid_s, sig = raw.split(".", 2)
        exp, uid = int(exp_s), int(uid_s)
    except (ValueError, AttributeError):
        return None
    now = time.time()
    if not (now <= exp <= now + PDF_TTL) or not (0 < uid <= 2_000_000_000):
        return None
    want = hmac.new(TOKEN.encode(), f"pdf:{uid}:{start}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return uid if hmac.compare_digest(sig, want) else None

def _pdf_link_uid(request):
    """Действующая подписная ссылка на ЭТОТ PDF → tg_id; иначе None."""
    if request.path != "/api/week.pdf":
        return None
    try:
        start = monday(request.query.get("start", "")).isoformat()
    except (ValueError, TypeError):
        return None
    return check_pdf_token(request.query.get("tok", ""), start)

def authorized(request):
    """200 — можно; 401 — подпись верна, но вход не выполнен; 403 — подделка/устарело/чужой.
    Три способа: X-Init (Telegram Mini App, подпись initData), X-Session (токен standalone-приложения)
    или подписная ссылка ?tok= — только для /api/week.pdf (см. pdf_token)."""
    try:
        raw = request.headers.get("X-Init", "")
        if not raw:
            ses = request.headers.get("X-Session", "")
            if ses:
                return 200 if auth.check_token(ses) else 403
            return 200 if _pdf_link_uid(request) else 403
        if len(raw) > 8192:
            return 403
        d = dict(parse_qsl(raw))
        got = d.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        want = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want.encode(), got.encode()):          # bytes: не падает на не-ASCII
            return 403
        age = time.time() - int(d["auth_date"])
        if age > INIT_MAX_AGE or age < -300:
            return 403
        usr = json.loads(d["user"])
        uid, uname = usr["id"], usr.get("username")
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return 403
    if type(uid) is not int or not auth.allowed(uid, uname):
        return 403
    return 200 if auth.is_authorized(uid) else 401

RATE_LIMIT = int(os.getenv("API_RATE_PER_MIN", "120"))      # запросов в минуту с одного аккаунта
_hits = {}                                                  # tg_id → времена последних запросов (только для вошедших)
_denied_log = {}

def rate_ok(uid, now=None):
    now = now or time.time()
    d = _hits.setdefault(uid, collections.deque())
    while d and d[0] < now - 60:
        d.popleft()
    if len(d) >= RATE_LIMIT:
        return False
    d.append(now)
    return True

def guarded(fn):
    async def w(request):
        code = authorized(request)
        if code != 200:
            if code == 403:                                  # посторонний с валидной подписью — пишем, но не чаще раза в 10 минут
                try: who = user_id(request)
                except Exception: who = None
                if time.time() - _denied_log.get(who, 0) > 600:
                    _denied_log[who] = time.time(); log.warning("api denied: tg_id=%s path=%s", who, request.path)
            return web.Response(status=code)
        uid = user_id(request)
        if not rate_ok(uid):
            log.warning("api rate limit: tg_id=%s path=%s", uid, request.path)
            return web.Response(status=429, headers={"Retry-After": "30"})
        try:
            resp = await fn(request)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError, OverflowError):
            log.info("api bad request: tg_id=%s path=%s", uid, request.path)
            return web.Response(status=400)
        except web.HTTPException:
            raise
        except Exception:                                    # непредвиденное: пишем в лог, наружу — без деталей
            log.exception("api error: tg_id=%s path=%s", uid, request.path)
            return web.json_response({"error": "internal"}, status=500)
        if request.method == "POST" and resp.status < 400:   # журнал изменений
            try:
                j = await request.json()
                target = " ".join(f"{k}={j[k]}" for k in AUDIT_KEYS if isinstance(j, dict) and isinstance(j.get(k), (int, str)) and len(str(j[k])) <= 20)
            except Exception:
                target = ""
            audit(uid, request.path, target)
        return resp
    return w

TIME_FMT = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]", re.ASCII)
DAY_FMT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
MAX_MONEY = 10_000_000
MAX_NAME, MAX_GRADE, MAX_SUBJECT = 100, 20, 100

def check_slot(j):
    """Проверяет день и время; день записывается обратно строго в виде YYYY-MM-DD (без «20261005» и т.п.)."""
    if not isinstance(j["day"], str) or not isinstance(j["time"], str) or not DAY_FMT.fullmatch(j["day"]):
        raise ValueError
    j["day"] = date.fromisoformat(j["day"]).isoformat()
    if not TIME_FMT.fullmatch(j["time"]):
        raise ValueError

def money_val(x, lo=0):
    """Целое число рублей в разумных пределах, иначе ValueError (→ 400)."""
    if isinstance(x, bool) or not isinstance(x, (int, str)):
        raise ValueError
    n = int(x)
    if not lo <= n <= MAX_MONEY:
        raise ValueError
    return n

def clip(s, limit):
    if not isinstance(s, str) or len(s.strip()) > limit:
        raise ValueError
    return s.strip()

@guarded
async def api_data(request):
    fill_regular()
    photos = {r["id"]: r["photo"] for r in q("select id, photo from students")}
    gs = {r["id"]: r for r in q("select id, grade, subject from students")}
    st = [{"id": s["id"], "name": s["name"], "price": s["price"], "paid": s["paid"],
           "done": s["done"], "spent": s["spent"], "balance": s["paid"] - s["spent"],
           "photo": photos.get(s["id"]), "deleted": s["deleted"], "grade": gs[s["id"]]["grade"] or "", "subject": gs[s["id"]]["subject"] or ""} for s in stats()]
    lessons = [dict(x) for x in q(f"""select l.id,l.day,l.time,coalesce(l.dur,60) dur,l.student_id,l.regular_id,s.name,
        coalesce(l.price,s.price) price, case when {PAST} then 1 else 0 end past from lessons l
        join students s on s.id=l.student_id order by l.day,l.time""")]
    pays = [dict(x) for x in q("""select p.id,p.student_id,p.amount,p.created,s.name from payments p
        join students s on s.id=p.student_id order by p.id desc limit 200""")]
    return web.json_response({"students": st, "lessons": lessons, "payments": pays, "cur": CUR})

def user_id(request):
    """Кто спрашивает: из подписи initData (Telegram), из токена X-Session (standalone)
    или из подписной ссылки на PDF (?tok=)."""
    try:
        uid = json.loads(dict(parse_qsl(request.headers.get("X-Init", ""))).get("user", "{}")).get("id")
    except (ValueError, RecursionError, TypeError):
        uid = None
    if uid is not None:
        return uid
    return auth.check_token(request.headers.get("X-Session", "")) or _pdf_link_uid(request)

def monday(s):
    if not isinstance(s, str) or not DAY_FMT.fullmatch(s):
        raise ValueError
    d = date.fromisoformat(s)
    return d - timedelta(days=d.weekday())

@guarded
async def api_week_pdf(request):
    start = monday(request.query["start"])
    pdf = week_pdf(start)
    # Оба заголовка обязательны для Telegram.WebApp.downloadFile (см. DownloadFileParams в доках Telegram):
    # без них нативное скачивание может молча не сработать, особенно на веб-платформах.
    return web.Response(body=pdf, content_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="raspisanie_{start}.pdf"',
                                 "Access-Control-Allow-Origin": "https://web.telegram.org"})

@guarded
async def api_week_send(request):
    """«Поделиться PDF» из мини-аппа. В WebView Telegram нет системного шаринга файлов (navigator.share с файлами)
    и не работает сохранение blob-ссылок, поэтому бот сам присылает PDF недели в чат с пользователем —
    оттуда файл пересылается любому контакту обычной кнопкой «Переслать»."""
    start = monday((await request.json())["start"])
    uid, tg_bot = user_id(request), request.app.get("bot")
    if not isinstance(uid, int) or tg_bot is None:
        return web.json_response({"error": "unavailable"}, status=503)
    doc, caption = week_doc(start)
    try:
        await tg_bot.send_document(uid, doc, caption=caption)
    except TelegramAPIError as e:
        log.warning("week pdf send failed: tg_id=%s err=%s", uid, e)
        return web.json_response({"error": "send_failed"}, status=502)
    return web.json_response({"ok": True})

@guarded
async def api_pdf_link(request):
    """Подписанная ссылка на PDF для нативного скачивания (Telegram ≥ 8.0 «Сохранить в Загрузки»
    и DownloadManager в APK): загрузчики идут по URL без заголовков авторизации."""
    start = monday(request.query["start"]).isoformat()
    uid = user_id(request)
    return web.json_response({"url": f"/api/week.pdf?start={start}&tok={pdf_token(uid, start)}"})

@guarded
async def api_add(request):
    j = await request.json(); check_slot(j)
    sid = int(j["student_id"])
    if not q("select 1 from students where id=? and coalesce(deleted,0)=0", sid):
        return web.json_response({"error": "no_student"}, status=404)
    res = add_lesson(sid, j["day"], j["time"], bool(j.get("regular")),
                     max(45, min(480, int(j.get("dur") or 60))))
    if res is None:
        return web.json_response({"error": "busy"}, status=409)
    return web.json_response({"ok": True, "skipped": res[1]})

@guarded
async def api_move(request):
    j = await request.json(); check_slot(j)
    dur = int(j.get("dur") or 0)
    dur = max(45, min(480, dur)) if dur else None
    if not move_lesson(int(j["id"]), j["day"], j["time"], dur):
        return web.json_response({"error": "busy"}, status=409)
    return web.json_response({"ok": True})

@guarded
async def api_cancel(request):
    cancel_lesson(int((await request.json())["id"]))
    return web.json_response({"ok": True})

@guarded
async def api_regular(request):
    return web.json_response({"ok": True, "skipped": make_regular(int((await request.json())["id"]))})

@guarded
async def api_stop(request):
    stop_series(int((await request.json())["id"]))
    return web.json_response({"ok": True})

MAX_PHOTO = 700_000   # ограничение размера base64-фото (~500 КБ картинки)
PHOTO_RE = re.compile(r"data:image/jpeg;base64,[A-Za-z0-9+/]+={0,2}")

def check_photo(photo):
    """None/"" (нет/убрать) или строго JPEG data-URL. Иначе — ValueError → 400."""
    if photo in (None, ""):
        return photo
    if not isinstance(photo, str) or len(photo) > MAX_PHOTO or not PHOTO_RE.fullmatch(photo):
        raise ValueError
    return photo

@guarded
async def api_student_update(request):
    j = await request.json()
    name = clip(j["name"], MAX_NAME) if "name" in j else None
    if name is not None and not name:
        return web.json_response({"error": "empty_name"}, status=400)
    photo = check_photo(j.get("photo"))
    sid = int(j["id"])
    price = money_val(j["price"]) if "price" in j else None
    extra = {k: clip(j[k] or "", lim) or None for k, lim in (("grade", MAX_GRADE), ("subject", MAX_SUBJECT)) if k in j}
    update_student(sid, name, photo)               # все проверки выше — до первой записи в БД
    if price is not None:
        set_price(sid, price)
    for k, v in extra.items():                     # k — только из белого списка выше
        run(f"update students set {k}=? where id=?", v, sid)
    return web.json_response({"ok": True})

@guarded
async def api_student_add(request):
    j = await request.json(); name = clip(j["name"], MAX_NAME)
    if not name:
        return web.json_response({"error": "empty_name"}, status=400)
    photo = check_photo(j.get("photo"))
    price = money_val(j.get("price") or 0)
    grade, subject = clip(j.get("grade") or "", MAX_GRADE) or None, clip(j.get("subject") or "", MAX_SUBJECT) or None
    sid = run("insert into students(name,price,grade,subject) values(?,?,?,?)", name, price, grade, subject)
    if photo:
        update_student(sid, None, photo)
    return web.json_response({"ok": True, "id": sid})

@guarded
async def api_student_delete(request):
    """«Мягкое» удаление: ученик пропадает из списков, прошлые занятия и оплаты остаются в истории,
    будущие занятия и регулярные серии убираются."""
    sid = int((await request.json())["id"])
    db.execute("update students set deleted=1 where id=?", (sid,))
    db.execute(f"delete from lessons where student_id=? and datetime(day||' '||time) > datetime('now','localtime')", (sid,))
    db.execute("delete from skips where regular_id in (select id from regular where student_id=?)", (sid,))
    db.execute("delete from regular where student_id=?", (sid,))
    db.commit()
    return web.json_response({"ok": True})

@guarded
async def api_payment_add(request):
    """Новая оплата из мини-аппа — в ту же таблицу payments, что и из бота (record_payment)."""
    j = await request.json()
    sid, amount = int(j["student_id"]), money_val(j["amount"], 1)
    if not q("select 1 from students where id=?", sid):
        return web.json_response({"error": "no_student"}, status=404)
    pid = run("insert into payments(student_id,amount) values(?,?)", sid, amount)
    return web.json_response({"ok": True, "id": pid})

@guarded
async def api_payment_update(request):
    j = await request.json()
    amount = money_val(j["amount"], 1)
    update_payment(int(j["id"]), amount)
    return web.json_response({"ok": True})

@guarded
async def api_payment_delete(request):
    delete_payment(int((await request.json())["id"]))
    return web.json_response({"ok": True})

# ---------- голосовой помощник ----------
# Мини-апп шлёт запись и КОНТЕКСТ: на какой вкладке и экране пользователь нажал «Голосовая команда»,
# какой фильтр/период выбран, какая карточка открыта. Контекст приходит от клиента, поэтому проверяется
# по белому списку: лишние ключи отбрасываются, неверный тип или значение → 400.
VOICE_TABS = {"c": "Расписание", "p": "Оплаты", "s": "Ученики", "ch": "Чат", "mt": "Материалы", "kn": "Канал"}
VOICE_SCREENS = {"lesson_form", "day_card", "student_card", "student_new", "payment_card", "payment_edit",
                 "payment_new", "balance", "debts", "print_preview", "grade_filter", "period_calendar",
                 "payment_student_pick", "payment_sort"}
VOICE_VIEWS = {"w", "m"}
VOICE_MAX_B64 = 1_500_000            # ~1,1 МБ звука; запись на фронте ограничена 45 секундами и сжата (~150 КБ)
VOICE_MAX_SEC = 60

def _int_id(x):
    if isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 2_000_000_000:
        raise ValueError
    return x

def _day(x):
    if not isinstance(x, str) or not DAY_FMT.fullmatch(x):
        raise ValueError
    return date.fromisoformat(x).isoformat()

def voice_context(raw):
    """Очищенный контекст страницы: {tab, tab_name, screen, screen_args, ...поля вкладки}."""
    if not isinstance(raw, dict) or raw.get("tab") not in VOICE_TABS:
        raise ValueError
    tab = raw["tab"]
    ctx = {"tab": tab, "tab_name": VOICE_TABS[tab], "screen": None, "screen_args": []}
    scr = raw.get("screen")
    if scr is not None:
        if scr not in VOICE_SCREENS:
            raise ValueError
        ctx["screen"] = scr
        args = raw.get("screen_args") or []
        if not isinstance(args, list) or len(args) > 4:
            raise ValueError
        for a in args:                                   # id (число) или день (YYYY-MM-DD); остальное — мимо
            try: ctx["screen_args"].append(_int_id(a))
            except ValueError: ctx["screen_args"].append(_day(a))
    if tab == "c":
        if raw.get("view") is not None:
            if raw["view"] not in VOICE_VIEWS: raise ValueError
            ctx["view"] = raw["view"]
        for k in ("week_start", "month"):
            if raw.get(k) is not None: ctx[k] = _day(raw[k])
    elif tab == "p":
        if raw.get("student_id") not in (None, "all"): ctx["student_id"] = _int_id(raw["student_id"])
        for k in ("date_from", "date_to"):
            if raw.get(k): ctx[k] = _day(raw[k])
    elif tab == "s":
        g = raw.get("filter")
        if g is not None:
            if not isinstance(g, str) or len(g) > MAX_GRADE: raise ValueError
            ctx["filter"] = g
    return ctx

def voice_audio(j):
    """(байты звука | None, mime). Звук необязателен: можно прислать уже распознанный текст."""
    import base64, binascii
    b64 = j.get("audio")
    if b64 in (None, ""):
        return None, ""
    mime = j.get("mime") or ""
    if not isinstance(b64, str) or len(b64) > VOICE_MAX_B64 or not isinstance(mime, str) or not mime.startswith("audio/") or len(mime) > 60:
        raise ValueError
    try:
        return base64.b64decode(b64, validate=True), mime
    except binascii.Error:
        raise ValueError

def voice_handle(ctx, audio, mime, text):
    """Точка расширения. Сейчас — заглушка: подключить распознавание речи (audio → text) и разбор
    команды (text + ctx → действие) можно здесь, не трогая фронт и проверку контекста."""
    where = ctx["tab_name"] + (f" · {ctx['screen']}" if ctx["screen"] else "")
    return {"stage": "stub", "reply": f"Команда получена на экране «{where}». Распознавание речи пока не подключено."}

@guarded
async def api_voice(request):
    j = await request.json()
    if not isinstance(j, dict):
        raise ValueError
    ctx = voice_context(j.get("ctx"))
    audio, mime = voice_audio(j)
    text = j.get("text")
    if text is not None and (not isinstance(text, str) or len(text) > 1000):
        raise ValueError
    dur = j.get("dur")
    if dur is not None and (isinstance(dur, bool) or not isinstance(dur, (int, float)) or not 0 <= dur <= VOICE_MAX_SEC):
        raise ValueError
    if audio is None and not text:
        raise ValueError
    res = voice_handle(ctx, audio, mime, (text or "").strip())
    log.info("voice: tg_id=%s tab=%s screen=%s audio=%sB", user_id(request), ctx["tab"], ctx["screen"], len(audio or b""))
    return web.json_response({"ok": True, "context": ctx, **res})

# ---------- CORS (нужно только если мини-апп на другом домене, напр. GitHub Pages) ----------
# Включается переменной ALLOW_ORIGIN — список разрешённых источников через запятую,
# либо "*". Если ALLOW_ORIGIN не задан, заголовки не добавляются и всё работает как раньше
# (страница и API на одном домене).
@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/api/") or request.path == "/":
        resp.headers.setdefault("Cache-Control", "no-store")   # данные не кешируем; и сама страница — иначе WebView отдаёт старый фронт после редеплоя
    allow = os.getenv("ALLOW_ORIGIN", "")
    origin = request.headers.get("Origin", "")
    if allow and origin and (allow == "*" or origin in [o.strip() for o in allow.split(",")]):
        resp.headers["Access-Control-Allow-Origin"] = "*" if allow == "*" else origin
        resp.headers["Access-Control-Allow-Headers"] = "X-Init, X-Session, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Vary"] = "Origin"
    return resp


def make_app():
    """Собирает веб-приложение (страница, /login и API). Вынесено из main() ради тестов."""
    app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[cors_mw])
    app.router.add_get("/", lambda _: web.FileResponse(os.path.join(HERE, "docs", "index.html")))
    app.router.add_get("/api/data", api_data)
    app.router.add_get("/api/week.pdf", api_week_pdf)
    app.router.add_get("/api/pdf_link", api_pdf_link)
    app.router.add_post("/api/week/send", api_week_send)
    app.router.add_post("/api/lesson/add", api_add)
    app.router.add_post("/api/lesson/move", api_move)
    app.router.add_post("/api/lesson/cancel", api_cancel)
    app.router.add_post("/api/lesson/regular", api_regular)
    app.router.add_post("/api/lesson/stop", api_stop)
    app.router.add_post("/api/student/update", api_student_update)
    app.router.add_post("/api/student/add", api_student_add)
    app.router.add_post("/api/student/delete", api_student_delete)
    app.router.add_post("/api/payment/add", api_payment_add)
    app.router.add_post("/api/payment/update", api_payment_update)
    app.router.add_post("/api/payment/delete", api_payment_delete)
    app.router.add_post("/api/voice", api_voice)
    return app


# ---------- keep-alive: не даём Render (free plan) усыпить сервис ----------
KA_SEC = int(os.getenv("KEEPALIVE_SEC", "180"))   # 3 минуты — чаще, чем 15-минутный простой Render

async def keepalive():
    """Каждые KA_SEC секунд шлёт HTTP-запрос на собственный ПУБЛИЧНЫЙ URL.
    KEEPALIVE_SEC=0 — выключено (так настроен dev: сервис засыпает без трафика и не тратит бесплатные часы).
    Render считает сервис активным только по входящим запросам через свой прокси,
    поэтому важны две вещи:
      1) адрес — именно WEBAPP_URL (https://....onrender.com/), а не 127.0.0.1:
         локальные запросы через прокси не проходят и активностью не считаются;
      2) интервал < 15 минут (по умолчанию 180 с). Меняется переменной KEEPALIVE_SEC.
    Недоступность адреса не роняет задачу: ошибка логируется, цикл продолжается."""
    base = (URL or "").rstrip("/")
    if not base:
        log.warning("keepalive выключен: не задан WEBAPP_URL")
        return
    import aiohttp
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                async with s.get(base + "/", timeout=aiohttp.ClientTimeout(total=15)) as r:
                    await r.read()
                log.debug("keepalive ping -> %s", r.status)
            except Exception as e:
                log.warning("keepalive ping не прошёл: %s", e)
            await asyncio.sleep(KA_SEC)


async def main():
    auth.require_config()
    fill_regular()
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(pub)
    dp.include_router(r)
    app = make_app()
    app["bot"] = bot

    async def on_login(tg_id):                      # после входа через сайт — открываем меню в чате
        await bot.send_message(tg_id, "✅ Вход выполнен. Готово к работе — выберите действие в меню.", reply_markup=menu_kb())
    async def on_alert(tg_id, text):
        await bot.send_message(tg_id, text)
    auth.setup(app, on_login, on_alert)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    ka = asyncio.create_task(keepalive())   # анти-сон: пинг каждые 3 мин (ссылку держим, чтобы задачу не собрал GC)
    if URL:
        btn = MenuButtonWebApp(text="Кабинет", web_app=WebAppInfo(url=URL))   # синяя кнопка слева от строки ввода
        await bot.set_chat_menu_button(menu_button=btn)
        for uid in sorted(auth.ALLOWED_IDS):
            try: await bot.set_chat_menu_button(chat_id=uid, menu_button=btn)
            except Exception: pass                                           # чат ещё не начат — не страшно
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
