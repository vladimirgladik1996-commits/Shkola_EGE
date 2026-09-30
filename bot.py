import asyncio, hashlib, hmac, json, os, re, sqlite3
from datetime import date, timedelta
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, KeyboardButton,
                           MenuButtonWebApp, Message, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.getenv("OWNER_ID", "0"))      # Telegram ID репетитора
URL = os.getenv("WEBAPP_URL", "")            # https-адрес мини-аппа
PORT = int(os.getenv("PORT", "8080"))
CUR = os.getenv("CURRENCY", "₽")
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
PER = 6
WEEKS_AHEAD = 12   # на сколько недель вперёд раскладываются регулярные занятия

# ---------- БД ----------
db = sqlite3.connect("tutor.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
create table if not exists students(id integer primary key, name text not null, price integer default 0, photo text);
create table if not exists lessons(id integer primary key, student_id integer not null, day text not null, time text not null,
  regular_id integer, dur integer default 60, unique(day,time));
create table if not exists payments(id integer primary key, student_id integer not null, amount integer not null,
  created text default (datetime('now','localtime')));
create table if not exists regular(id integer primary key, student_id integer not null, weekday integer not null,
  time text not null, start text not null, dur integer default 60);
create table if not exists skips(regular_id integer, day text, primary key(regular_id, day));
""")
for stmt in ("alter table lessons add column regular_id integer",
             "alter table students add column photo text",
             "alter table lessons add column dur integer default 60",
             "alter table regular add column dur integer default 60"):     # для старых баз
    try:
        db.execute(stmt)
    except sqlite3.OperationalError:
        pass

def q(sql, *a): return db.execute(sql, a).fetchall()
def run(sql, *a):
    c = db.execute(sql, a); db.commit(); return c.lastrowid

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
def stats():
    return q("""select s.id, s.name, s.price,
      coalesce((select sum(amount) from payments where student_id=s.id),0) paid,
      (select count(*) from lessons where student_id=s.id
         and datetime(day||' '||time) <= datetime('now','localtime')) done
      from students s order by s.name""")

def balance_text(sid):
    s = next(x for x in stats() if x["id"] == sid)
    spent = s["done"] * s["price"]
    bal = s["paid"] - spent
    lines = [f"Оплачено всего: {money(s['paid'])}"]
    if s["price"]:
        lines.append(f"Проведено занятий: {s['done']} × {money(s['price'])} = {money(spent)}")
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

# ---------- PDF расписания ----------
import io, os
from reportlab.lib.colors import Color, black
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

HERE = os.path.dirname(os.path.abspath(__file__))

def _font(name, fname):
    for p in (f"{HERE}/fonts/{fname}", f"/usr/share/fonts/truetype/dejavu/{fname}"):
        if os.path.exists(p):
            pdfmetrics.registerFont(TTFont(name, p))
            return
    raise RuntimeError(f"Не найден шрифт {fname}: положите его в папку fonts/")

_font("TS", "DejaVuSans.ttf")
_font("TSB", "DejaVuSans-Bold.ttf")

WDFULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

def week_pdf(start):
    """A4 альбомная: сетка время × 7 дней. Чёрно-белый дизайн — вместо цвета
    у занятых ячеек белая заливка и жирная левая полоса, у пустых — светло-серая
    полоса по всей строке (для навигации по часам). Ничего не зависит от цвета."""
    fill_regular()
    days = [start + timedelta(days=i) for i in range(7)]
    rows = q("""select l.day,l.time,coalesce(l.dur,60) dur,l.student_id,l.regular_id,s.name from lessons l
                join students s on s.id=l.student_id where l.day between ? and ? order by l.time""",
             days[0].isoformat(), days[-1].isoformat())
    cell = {}
    for x in rows:
        cell.setdefault((x["day"], int(x["time"][:2])), []).append(x)

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=landscape(A4))
    W, H = landscape(A4)
    M, TC, HEAD = 30, 62, 34
    TITLE = 46
    hours = list(range(8, 21))
    colw = (W - 2 * M - TC) / 7
    grid_top = H - M - TITLE
    grid_bottom = M
    rh = (grid_top - HEAD - grid_bottom) / len(hours)

    band = Color(.91, .91, .91)      # нейтральный серый для пустых строк — не путается с текстом
    line = Color(.55, .55, .55)
    x0 = M + TC

    # заголовок
    c.setFillColor(black); c.setFont("TSB", 20); c.drawString(M, H - M - 18, "Расписание на неделю")
    c.setFillColor(Color(.35, .35, .35)); c.setFont("TS", 11)
    c.drawString(M, H - M - 35, f"{days[0]:%d.%m.%Y} – {days[-1]:%d.%m.%Y}")

    # шапка дней
    c.setLineWidth(.7); c.setStrokeColor(line)
    for i, d in enumerate(days):
        x = x0 + i * colw
        c.setFillColor(black); c.setFont("TSB", 12.5)
        c.drawCentredString(x + colw / 2, grid_top - 15, WDFULL[i])
        c.setFillColor(Color(.4, .4, .4)); c.setFont("TS", 10)
        c.drawCentredString(x + colw / 2, grid_top - 29, f"{d:%d.%m.%Y}")
    c.setLineWidth(1.3); c.setStrokeColor(black)
    c.line(M, grid_top - HEAD, W - M, grid_top - HEAD)

    # сетка: линии часов и блоки занятий по минутам (высота = длительность)
    from reportlab.lib.utils import simpleSplit
    top0 = grid_top - HEAD
    c.setFillColor(band); c.rect(x0, grid_bottom, colw * 7, top0 - grid_bottom, fill=1, stroke=0)
    for j, h in enumerate(hours):
        y = top0 - j * rh
        c.setFillColor(black); c.setFont("TSB", 10); c.drawRightString(x0 - 8, y - 8, f"{h:02d}:00")
        c.setStrokeColor(line); c.setLineWidth(.6); c.line(x0, y, W - M, y)
    for x in rows:
        i = (date.fromisoformat(x["day"]) - days[0]).days
        s = int(x["time"][:2]) * 60 + int(x["time"][3:]); e = s + x["dur"]
        hgt = x["dur"] / 60 * rh; yt = top0 - (s - 480) / 60 * rh; bx = x0 + i * colw
        c.setFillColor(Color(1, 1, 1)); c.setStrokeColor(black); c.setLineWidth(.6)
        c.rect(bx, yt - hgt, colw, hgt, fill=1, stroke=1)
        c.setLineWidth(3.2); c.line(bx, yt - hgt, bx, yt)
        c.setFillColor(black)
        if hgt < 28:
            c.setFont("TSB", 8.5); c.drawString(bx + 6, yt - hgt / 2 - 3, f"{x['time']} {x['name']}"[:int((colw - 10) / 4.4)])
        else:
            c.setFont("TS", 8)
            c.drawString(bx + 6, yt - 10, f"{x['time']}–{e // 60:02d}:{e % 60:02d}" + (" · повтор" if x["regular_id"] else ""))
            c.setFont("TSB", 9.5)
            for k, t_ in enumerate(simpleSplit(x["name"], "TSB", 9.5, colw - 12)[:max(1, int((hgt - 14) // 11))]):
                c.drawString(bx + 6, yt - 22 - k * 11, t_)
    c.setLineWidth(1); c.setStrokeColor(black)
    for i in range(8):
        x = x0 + i * colw
        c.line(x, grid_top - HEAD, x, grid_bottom)
    c.line(M, grid_top, M, grid_bottom); c.line(W - M, grid_top, W - M, grid_bottom)

    c.setFillColor(Color(.5, .5, .5)); c.setFont("TS", 8)
    c.drawString(M, 14, f"Сформировано {date.today():%d.%m.%Y}")
    c.drawRightString(W - M, 14, "высота блока = длительность · полоса слева = занято · «повтор» = регулярное")
    c.save()
    return buf.getvalue()

# ---------- клавиатуры ----------
def menu_kb():
    b = ReplyKeyboardBuilder()
    if URL:
        b.row(KeyboardButton(text="📱 Открыть кабинет", web_app=WebAppInfo(url=URL)))
    b.row(KeyboardButton(text="📅 Новое занятие"), KeyboardButton(text="💳 Новая оплата"))
    b.row(KeyboardButton(text="👤 Новый ученик"))
    return b.as_markup(resize_keyboard=True)

def dates_kb(off):
    b = InlineKeyboardBuilder()
    start = date.today() + timedelta(days=off * 8)
    for i in range(8):
        d = start + timedelta(days=i)
        b.button(text=f"{WD[d.weekday()]} {d:%d.%m}", callback_data=f"lt:{d.isoformat()}")
    b.adjust(2)
    nav = []
    if off > 0: nav.append(InlineKeyboardButton(text="◀️", callback_data=f"ld:{off-1}"))
    nav.append(InlineKeyboardButton(text="▶️", callback_data=f"ld:{off+1}"))
    b.row(*nav)
    return b.as_markup()

def time_kb(day):
    busy = {r["time"] for r in q("select time from lessons where day=?", day)}
    b = InlineKeyboardBuilder()
    for h in range(8, 21):
        t = f"{h:02d}:00"
        b.button(text=("🔒 " if t in busy else "") + t,
                 callback_data="busy" if t in busy else f"lm:{day}:{h:02d}00")
    b.adjust(3)
    b.row(InlineKeyboardButton(text="🕐 Своё время (например 8:25)", callback_data=f"lo:{day}"))
    b.row(InlineKeyboardButton(text="◀️ К датам", callback_data="ld:0"))
    return b.as_markup()

def mode_kb(day, t):
    b = InlineKeyboardBuilder()
    b.button(text="Разовое занятие", callback_data=f"lc:{day}:{t}:o")
    b.button(text="🔁 Сделать регулярным", callback_data=f"lc:{day}:{t}:r")
    b.button(text="◀️ К времени", callback_data=f"lt:{day}")
    b.adjust(1)
    return b.as_markup()

def dur_kb(day, t):
    b = InlineKeyboardBuilder()
    for d in (30, 45, 60, 90, 120):
        b.button(text=f"{d} мин", callback_data=f"lq:{day}:{t}:{d}")
    b.adjust(3)
    return b.as_markup()

def students_kb(pick, more, page, new_cb):
    rows = q("select id,name from students order by name")
    b = InlineKeyboardBuilder()
    for r_ in rows[page * PER:(page + 1) * PER]:
        b.button(text=r_["name"], callback_data=f"{pick}:{r_['id']}")
    b.adjust(2)
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
    return b.as_markup()

def price_kb():
    b = InlineKeyboardBuilder()
    for a in (2000, 3000, 4000, 5000):
        b.button(text=f"{a:,}".replace(",", " "), callback_data=f"ns:{a}")
    b.button(text="Без цены", callback_data="ns:0")
    b.adjust(4, 1)
    return b.as_markup()

# ---------- состояния ----------
class NewStudent(StatesGroup):
    name = State()
    price = State()

class CustomTime(StatesGroup):
    day = State()

class Pay(StatesGroup):
    amount = State()

r = Router()
if OWNER:
    r.message.filter(F.from_user.id == OWNER)
    r.callback_query.filter(F.from_user.id == OWNER)

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
        text = f"✅ {name} записан(а): {pretty(day, t)}, {dur} мин"
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

async def student_done(m: Message, state: FSMContext, name, price):
    then = (await state.get_data()).get("then")
    sid = run("insert into students(name,price) values(?,?)", name, price)
    await state.clear()
    await m.answer(f"✅ Ученик «{name}» добавлен" + (f", занятие {money(price)}" if price else ""))
    if then == "lesson":
        await state.update_data(sid=sid)
        await m.answer("Выберите дату:", reply_markup=dates_kb(0))
    elif then == "pay":
        await ask_amount(m, state, sid, False)
    elif then and then.startswith("l:"):
        _, day, t, mode = then.split(":")
        await make_lesson(m, day, hm(t), sid, False, mode == "r")

# ---------- меню ----------
@r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Готово к работе. Выберите действие в меню.", reply_markup=menu_kb())

@r.message(F.text == "📅 Новое занятие")
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
    await state.set_state(NewStudent.name)
    await state.update_data(then="lesson")
    await c.message.answer("Как зовут ученика?")
    await c.answer()

@r.callback_query(F.data.startswith("lq:"))
async def c_dur(c: CallbackQuery, state: FSMContext):
    _, day, t, d = c.data.split(":")
    await state.update_data(dur=int(d))
    await c.message.edit_text(f"{pretty(day, hm(t))}, {d} мин\nКакое занятие?", reply_markup=mode_kb(day, t))

@r.message(F.text == "💳 Новая оплата")
async def new_pay(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Выберите ученика:", reply_markup=students_kb("pa", "pu", 0, "pn"))

@r.message(F.text == "👤 Новый ученик")
async def new_student(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(NewStudent.name)
    await m.answer("Как зовут ученика?")

# ---------- занятие ----------
@r.callback_query(F.data.startswith("ld:"))
async def c_dates(c: CallbackQuery):
    await c.message.edit_text("Выберите дату:", reply_markup=dates_kb(int(c.data[3:])))

@r.callback_query(F.data.startswith("lt:"))
async def c_times(c: CallbackQuery):
    fill_regular()
    day = c.data[3:]
    d = date.fromisoformat(day)
    await c.message.edit_text(f"{WD[d.weekday()]} {d:%d.%m} — выберите время:", reply_markup=time_kb(day))

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
    await c.message.answer(f"{WD[d.weekday()]} {d:%d.%m} — введите время в формате ЧЧ:ММ, например 8:25")
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
    await state.set_state(NewStudent.name)
    await state.update_data(then=f"l:{day}:{t}:{mode}")
    await c.message.answer("Как зовут ученика?")
    await c.answer()

# ---------- оплата ----------
@r.callback_query(F.data.startswith("pu:"))
async def c_pay_students(c: CallbackQuery):
    await c.message.edit_text("Выберите ученика:",
                              reply_markup=students_kb("pa", "pu", int(c.data[3:]), "pn"))

@r.callback_query(F.data == "pn")
async def c_pay_new_student(c: CallbackQuery, state: FSMContext):
    await state.set_state(NewStudent.name)
    await state.update_data(then="pay")
    await c.message.answer("Как зовут ученика?")
    await c.answer()

@r.callback_query(F.data.startswith("pa:"))
async def c_pay_amount(c: CallbackQuery, state: FSMContext):
    await ask_amount(c.message, state, int(c.data[3:]), True)

async def record_payment(m: Message, state: FSMContext, sid, amount, edit):
    run("insert into payments(student_id,amount) values(?,?)", sid, amount)
    name = q("select name from students where id=?", sid)[0]["name"]
    await state.clear()
    await (m.edit_text if edit else m.answer)(
        f"✅ Оплата принята: +{money(amount)}\n\n👤 {name}\n{balance_text(sid)}")

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

# ---------- новый ученик ----------
@r.message(NewStudent.name, F.text)
async def m_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(NewStudent.price)
    await m.answer("Стоимость одного занятия? (нужна для расчёта долгов)", reply_markup=price_kb())

@r.callback_query(NewStudent.price, F.data.startswith("ns:"))
async def c_price(c: CallbackQuery, state: FSMContext):
    name = (await state.get_data())["name"]
    await c.message.edit_reply_markup()
    await student_done(c.message, state, name, int(c.data[3:]))

@r.message(NewStudent.price, F.text.regexp(r"^\d[\d\s]*$"))
async def m_price(m: Message, state: FSMContext):
    name = (await state.get_data())["name"]
    await student_done(m, state, name, int(m.text.replace(" ", "")))

# ---------- API для мини-аппа ----------
def authorized(request):
    d = dict(parse_qsl(request.headers.get("X-Init", "")))
    got = d.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    ok = hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), got)
    uid = json.loads(d.get("user", "{}")).get("id")
    return ok and (not OWNER or uid == OWNER)

def guarded(fn):
    async def w(request):
        if not authorized(request):
            return web.Response(status=403)
        try:
            return await fn(request)
        except (ValueError, KeyError, IndexError):
            return web.Response(status=400)
    return w

TIME_FMT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

def check_slot(j):
    date.fromisoformat(j["day"])
    if not TIME_FMT.match(j["time"]):
        raise ValueError

@guarded
async def api_data(request):
    fill_regular()
    photos = {r["id"]: r["photo"] for r in q("select id, photo from students")}
    st = [{"id": s["id"], "name": s["name"], "price": s["price"], "paid": s["paid"],
           "done": s["done"], "balance": s["paid"] - s["done"] * s["price"],
           "photo": photos.get(s["id"])} for s in stats()]
    lessons = [dict(x) for x in q("""select l.id,l.day,l.time,coalesce(l.dur,60) dur,l.student_id,l.regular_id,s.name from lessons l
        join students s on s.id=l.student_id order by l.day,l.time""")]
    pays = [dict(x) for x in q("""select p.id,p.student_id,p.amount,p.created,s.name from payments p
        join students s on s.id=p.student_id order by p.id desc limit 200""")]
    return web.json_response({"students": st, "lessons": lessons, "payments": pays, "cur": CUR})

def user_id(request):
    return json.loads(dict(parse_qsl(request.headers.get("X-Init", ""))).get("user", "{}")).get("id")

def monday(s):
    d = date.fromisoformat(s)
    return d - timedelta(days=d.weekday())

@guarded
async def api_week_pdf(request):
    pdf = week_pdf(monday(request.query["start"]))
    return web.Response(body=pdf, content_type="application/pdf")

@guarded
async def api_week_send(request):
    start = monday((await request.json())["start"])
    pdf = week_pdf(start)
    await request.app["bot"].send_document(
        user_id(request), BufferedInputFile(pdf, filename=f"raspisanie_{start}.pdf"),
        caption=f"Расписание на неделю {start:%d.%m} – {start + timedelta(days=6):%d.%m}. Перешлите файл в нужный мессенджер.")
    return web.json_response({"ok": True})

@guarded
async def api_add(request):
    j = await request.json(); check_slot(j)
    res = add_lesson(int(j["student_id"]), j["day"], j["time"], bool(j.get("regular")),
                     max(15, min(480, int(j.get("dur") or 60))))
    if res is None:
        return web.json_response({"error": "busy"}, status=409)
    return web.json_response({"ok": True, "skipped": res[1]})

@guarded
async def api_move(request):
    j = await request.json(); check_slot(j)
    if not move_lesson(int(j["id"]), j["day"], j["time"], int(j.get("dur") or 0) or None):
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

@guarded
async def api_student_update(request):
    j = await request.json()
    name = j["name"].strip() if "name" in j else None
    if name is not None and not name:
        return web.json_response({"error": "empty_name"}, status=400)
    photo = j.get("photo")
    if photo and len(photo) > MAX_PHOTO:
        return web.json_response({"error": "photo_too_big"}, status=400)
    update_student(int(j["id"]), name, photo)
    if "price" in j:
        run("update students set price=? where id=?", max(0, int(j["price"])), int(j["id"]))
    return web.json_response({"ok": True})

@guarded
async def api_student_add(request):
    j = await request.json(); name = j["name"].strip()
    if not name:
        return web.json_response({"error": "empty_name"}, status=400)
    sid = run("insert into students(name,price) values(?,?)", name, int(j.get("price") or 0))
    return web.json_response({"ok": True, "id": sid})

@guarded
async def api_payment_update(request):
    j = await request.json()
    amount = int(j["amount"])
    if amount <= 0:
        return web.json_response({"error": "bad_amount"}, status=400)
    update_payment(int(j["id"]), amount)
    return web.json_response({"ok": True})

@guarded
async def api_payment_delete(request):
    delete_payment(int((await request.json())["id"]))
    return web.json_response({"ok": True})

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
    allow = os.getenv("ALLOW_ORIGIN", "")
    origin = request.headers.get("Origin", "")
    if allow and origin and (allow == "*" or origin in [o.strip() for o in allow.split(",")]):
        resp.headers["Access-Control-Allow-Origin"] = "*" if allow == "*" else origin
        resp.headers["Access-Control-Allow-Headers"] = "X-Init, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Vary"] = "Origin"
    return resp


async def main():
    fill_regular()
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(r)
    app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[cors_mw])
    app["bot"] = bot
    app.router.add_get("/", lambda _: web.FileResponse("webapp/index.html"))
    app.router.add_get("/api/data", api_data)
    app.router.add_get("/api/week.pdf", api_week_pdf)
    app.router.add_post("/api/week/send", api_week_send)
    app.router.add_post("/api/lesson/add", api_add)
    app.router.add_post("/api/lesson/move", api_move)
    app.router.add_post("/api/lesson/cancel", api_cancel)
    app.router.add_post("/api/lesson/regular", api_regular)
    app.router.add_post("/api/lesson/stop", api_stop)
    app.router.add_post("/api/student/update", api_student_update)
    app.router.add_post("/api/student/add", api_student_add)
    app.router.add_post("/api/payment/update", api_payment_update)
    app.router.add_post("/api/payment/delete", api_payment_delete)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    if URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Кабинет", web_app=WebAppInfo(url=URL)))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
