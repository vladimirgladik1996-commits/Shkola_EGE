"""«ИИ-проверка»: репродукции баг T-01..T-06 (план очистки раздела — PLAN-IIPROVERKA.md).

Тесты НАМЕРЕННО красные до фиксов F-1..F-6: каждый прогон падает и ПОКАЗЫВАЕТ причину.
Конвенция плана: пуш 1 = документы + красные тесты; далее каждый фикс гасит свои T-xx.

  T-01 «Ложное "нет интернет"»: ecUpApi при HTTP 400/404/500 обязан возвращать
       {ok,status,body} (а не null), маппинг текста ошибки по коду сервера
       (gone→«перезаливаю», bad→«файл не распознан», 5xx→«сервис перегружен»);
       «нет интернет» — ТОЛЬКО при реальном обрыве (onerror/onabort/timeout).
       Бэкенд: /api/essay_check/photo обязан различать причины (413/429/400 + код),
       а не отвечать универсальным {"error":"bad"} на всё (F-6).
  T-02 «Кнопка "Готово" разъезжается»: подпись кнопки НЕ мутирует (нет
       btn0.textContent="Загружаю фото…"): перенос подписи на 2 строки ломал сетку
       .ec-ctl (1fr 64px 1fr). Busy-состояние — класс/атрибут + render().
  T-03 «Утечка EC_PHOTOS при повторе»: повторная догрузка тех же фото не должна
       наращивать копилку (len(store) растёт → >= EC_MAX_PHOTOS → ложный 400 «bad»
       → ложное «нет интернет»). Повтор идемпотентен, мусор вычищается.
  T-04 «Прелоадер не срабатывает»: при любом сбое загрузки пользователь получает
       ЯВНУЮ ошибку с кнопкой «Повторить» (не только тост), а не остаётся на экране
       съёмки без объяснений; кнопка восстанавливается в любом исходе (включая throw).
  T-05 «Гонка фоновой загрузки»: ecUpKick() и ecUpAll() на одно фото = одна отправка
       в полёте (p.jn); успешно загруженное фото (st=ok/id) повтор НЕ переотправляет
       без честного «gone» от сервера — «одна отправка на фото».
  T-06 «Протухший was»: восстановление кнопки — не из сохранённого снимка текста
       (b.textContent=was в узел, который мог пересоздать render()), а каноническое
       состояние кнопки/через render().

Запуск: python3 tests/test_essay_check_bugs.py   (пока красный — это НОРМА этого пуша)
"""
import asyncio, base64, hashlib, hmac, io, json, os, shutil, sqlite3, subprocess, sys, time
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="test-token-not-secret", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
_db_existed = os.path.exists(os.path.join(ROOT, "data", "tutor.db"))
import auth, bot, chatfiles
from aiohttp.test_utils import TestClient, TestServer

RED = []                                          # накопленные красные места (пока фиксов нет)

def t(cond, label):
    if cond:
        print("  ✓", label)
    else:
        print("  ✗ КРАСНЫЙ:", label)
        RED.append(label)

def ok(c): print("  ✓", c)

html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
up_body = html[html.index("async function ecUpload(skip)"):html.index("function ecLogHtml(")]

mem = sqlite3.connect(":memory:", check_same_thread=False)
bot.init_db(mem)
bot.db = mem; auth.init(mem); auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp())); mem.commit()

def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", bot.TOKEN.encode(), hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)

H = {"X-Init": init(42, "Gladik_N")}

from PIL import Image
def jpg(color=(255, 255, 255)):
    im = Image.new("RGB", (32, 32), color)
    out = io.BytesIO(); im.save(out, "JPEG", quality=85)
    return base64.b64encode(out.getvalue()).decode()

JPG_W, JPG_T = jpg((255, 255, 255)), jpg((200, 200, 200))

# ---------- T-01а. ecUpApi: ответ сервера (400/500) — это {ok,status,body}, не null ----------
if not shutil.which("node"):
    print("  - node не найден — живой прогон ecUpApi пропущен")
else:
    src = html[html.index("function ecUpApi("):html.index("async function ecUpOne(")]
    harness = """
let APP=false, API_BASE="", tg={initData:"x"};
function lockScreen(){throw new Error("lockScreen не должен вызываться")}
const EC_UP_IDLE=150, EC_UP_MAX=400;
let mode="ok", aborted=0;
class XHR{
  constructor(){this.upload={};this.h={};this.status=0;this.responseText=""}
  open(m,u){this.m=m;this.u=u}
  setRequestHeader(k,v){this.h[k]=v}
  abort(){aborted++;if(this.onabort)this.onabort()}
  send(b){const s=this;
    if(mode=="ok"){s.status=200;s.responseText='{"id":"p1"}';s.onload&&s.onload();return}
    if(mode=="bad"){s.status=400;s.responseText='{"error":"bad"}';s.onload&&s.onload();return}
    if(mode=="gone"){s.status=400;s.responseText='{"error":"gone"}';s.onload&&s.onload();return}
    if(mode=="srv"){s.status=500;s.responseText="";s.onload&&s.onload();return}
    if(mode=="net"){s.onerror&&s.onerror();return}}
}
globalThis.XMLHttpRequest=XHR;
""" + src + """
(async()=>{
  const out={};
  for(const m of ["ok","bad","gone","srv","net"]){mode=m;out[m]=await ecUpApi("/x",{})}
  console.log("RESULT:"+JSON.stringify(out));
})().catch(e=>{console.log("ERR:"+e);process.exit(1)});
"""
    r = subprocess.run(["node"], input=harness, capture_output=True, text=True)
    t(r.returncode == 0, f"T-01: ecUpApi воспроизводим в node ({r.stderr[-300:]})")
    if r.returncode == 0:
        res = json.loads([ln for ln in r.stdout.splitlines() if ln.startswith("RESULT:")][0][7:])
        def shaped(v):
            return isinstance(v, dict) and "ok" in v and "status" in v and "body" in v
        t(all(shaped(res.get(m)) for m in ("bad", "gone", "srv", "net")),
          "T-01: ecUpApi при HTTP-ошибке/обрыве возвращает {ok,status,body}, а не null (иначе "
          "любой сбой сервера трактуется как «нет интернет»)")
        if shaped(res.get("bad")):
            t(res["bad"]["ok"] is False and res["bad"]["status"] == 400 and (res["bad"].get("body") or {}).get("error") == "bad",
              "T-01: HTTP 400 bad — статус и код причины доходят до вызывающего")
        if shaped(res.get("srv")):
            t(res["srv"]["status"] >= 500, "T-01: HTTP 5xx — статус доходит до вызывающего («сервис перегружен»)")
        if shaped(res.get("net")):
            t(res["net"]["status"] not in range(200, 300) and not res["net"].get("body"),
              "T-01: реальный обрыв (onerror) — без статуса/тела; ТОЛЬКО он имеет право давать «нет интернет»")

# ---------- T-01б. маппинг текстов ошибок по коду сервера (F-1) ----------
t("перезаливаю" in html and "не распознан" in html and "перегружен" in html,
  "T-01: тексты ошибок разделены по кодам сервера (gone→«перезаливаю», bad→«файл не распознан», "
  "5xx→«сервис перегружен»), а не общий «проверьте интернет»")

# ---------- T-01в. бэкенд: /api/essay_check/photo различает причины отказа (F-6) ----------
async def t01_backend():
    async with TestClient(TestServer(bot.make_app())) as c:
        r = await c.post("/api/essay_check/photo", json={"sid": "sess-t01-bugs", "set": "w", "d": "мусор!"}, headers=H)
        bad = await r.json()
        sid = "sess-t01-limit"
        for i in range(bot.EC_MAX_PHOTOS):
            await c.post("/api/essay_check/photo", json={"sid": sid, "set": "w", "d": JPG_W}, headers=H)
        r = await c.post("/api/essay_check/photo", json={"sid": sid, "set": "w", "d": JPG_W}, headers=H)
        lim = await r.json()
        t(r.status == 400 and bad.get("error") == "bad", "T-01: мусор вместо картинки → 400 {error:bad}")
        t(not (lim.get("error") == "bad" and r.status == 400),
          "T-01: переполнение копилки НЕ должно отвечать универсальным 400 «bad» — "
          f"нужен свой код/статус (413/429/limit), сейчас {r.status} {lim}")

asyncio.run(t01_backend())

# ---------- T-02. подпись «Готово» не мутирует — сетка .ec-ctl не разъезжается ----------
t('btn0.textContent="Загружаю фото…"' not in html and 'btn0.textContent=' not in up_body,
  "T-02: busy-состояние не пишет текст прямо в кнопку «Готово» (подпись в 2 строки разрывала "
  ".ec-ctl 1fr 64px 1fr) — состояние только классом/атрибутом + render()")
t("1fr 64px 1fr" in html or ".ec-ctl" in html, "T-02: сетка .ec-ctl на месте (регресс-страж)")
t("classList.add(\"busy\")" in html or "data-busy" in html or "ecBusy" in html,
  "T-02: busy-состояние существует (класс/атрибут), просто не через текст кнопки")

# ---------- T-03. повтор не наращивает копилку EC_PHOTOS (F-3) ----------
async def t03_backend():
    sid = "sess-t03-leak"
    bodies = (JPG_W, JPG_T)                        # 2 уникальных фото, повторы — те же байты
    posts, failed = 0, None
    for round_ in range(4):                        # клиент повторяет догрузку после сбоев
        for i in range(5):
            r = await c.post("/api/essay_check/photo", json={"sid": sid, "set": "w", "d": bodies[i % 2]}, headers=H)
            posts += 1
            if r.status != 200:
                failed = (posts, r.status, await r.json())
                break
        if failed:
            break
    t(failed is None,
      f"T-03: повтор одних и тех же фото не должен упираться в лимит/отклоняться — "
      f"сейчас {failed} после {posts} повторных загрузок (копилка растёт как мусор)")
    store = bot.EC_PHOTOS.get(sid, {})
    t(len(store) <= 2,
      f"T-03: копилка не копит дубли: при 2 уникальных фото в store {len(store)} записей "
      f"(>= EC_MAX_PHOTOS ({bot.EC_MAX_PHOTOS}) включает ложный 400 → ложное «нет интернет»)")

async def main():
    global c
    async with TestClient(TestServer(bot.make_app())) as client:
        c = client
        await t03_backend()
    print()
    if RED:
        print(f"test_essay_check_bugs: {len(RED)} КРАСНЫХ — ОЖИДАЕМО до фиксов F-1..F-6 (это репродукции):")
        for x in RED:
            print("   ✗", x)
        sys.exit(1)
    print("test_essay_check_bugs: OK")

# ---------- T-04. явная ошибка + «Повторить», кнопка восстанавливается в любом исходе ----------
t("Повторить" in up_body,
  "T-04: при сбое загрузки — ЯВНАЯ ошибка с кнопкой «Повторить», а не только тост "
  "(иначе пользователь остаётся на экране съёмки, прелоадер «не срабатывает»)")
t("finally{done()}" in up_body,
  "T-04: кнопка «Готово» восстанавливается в любом исходе, включая исключение (finally)")

# ---------- T-05. одна отправка на фото: гонка ecUpKick/ecUpAll + повтор ----------
if shutil.which("node"):
    src = html[html.index("function ecUpApi("):html.index("async function ecUpload(")]
    harness = """
let APP=false, API_BASE="", tg={initData:"x"};
function lockScreen(){throw new Error("lockScreen не должен вызываться")}
const EC_UP_IDLE=5000, EC_UP_MAX=9000;
let sends=[];                                     // каждая отправка: {body, finish(st,text)}
class XHR{
  constructor(){this.upload={};this.h={};this.status=0;this.responseText=""}
  open(m,u){this.m=m;this.u=u}
  setRequestHeader(k,v){this.h[k]=v}
  abort(){if(this.onabort)this.onabort()}
  send(b){const s=this;const rec={body:JSON.parse(b)};
    rec.finish=(st,txt)=>{s.status=st;s.responseText=txt;s.onload&&s.onload()};
    sends.push(rec)}
}
globalThis.XMLHttpRequest=XHR;
let ecShots={w:[],t:[]}, ecUpId="sess-t05";
""" + src + """
(async()=>{
  const out={};
  const p={src:"data:image/jpeg;base64,QQ==",set:"w",id:null,st:"wait"};
  ecShots={w:[p],t:[]};
  ecUpKick();                                    // фоновая загрузка после съёмки
  const j=ecUpAll();                             // тут же «Готово» дожимает то же фото
  out.sendsInFlight=sends.length;                // в полёте обязана быть ОДНА отправка
  sends[0].finish(200,'{"id":"p1"}');
  await j;
  out.after={sends:sends.length, st:p.st, id:p.id};
  // повтор «Готово» после сбоя сборки: как в ecUpload — сброс фото в wait/id=null
  p.st="wait"; p.id=null;
  await ecUpAll();
  out.afterRetry={sends:sends.length, st:p.st, id:p.id};
  console.log("RESULT:"+JSON.stringify(out));
})().catch(e=>{console.log("ERR:"+e);process.exit(1)});
"""
    r = subprocess.run(["node"], input=harness, capture_output=True, text=True)
    t(r.returncode == 0, f"T-05: гонка ecUpKick/ecUpAll воспроизводима в node ({r.stderr[-300:]})")
    if r.returncode == 0:
        res = json.loads([ln for ln in r.stdout.splitlines() if ln.startswith("RESULT:")][0][7:])
        t(res["sendsInFlight"] == 1 and res["after"]["sends"] == 1,
          f"T-05: одно фото = одна отправка в полёте (гонка ecUpKick+ecUpAll не дублирует): {res}")
        t(res["after"]["st"] == "ok" and res["after"]["id"] == "p1",
          f"T-05: результат полёта не теряется после сброса p.st — фото ok/id=p1: {res}")
        t(res["afterRetry"]["sends"] == 1,
          f"T-05: повтор НЕ переотправляет уже загруженное фото без «gone» от сервера "
          f"(двойная отправка = дубли в EC_PHOTOS и ложные 400): {res}")

# ---------- T-06. восстановление кнопки не из «протухшего» снимка текста ----------
t("was=btn0&&btn0.textContent" not in html and "b.textContent=was" not in html and "btn0.textContent=was" not in html,
  "T-06: кнопка восстанавливается каноническим состоянием/render(), а не текстом, "
  "сохранённым ДО render() (снимок «was» писался в пересозданный узел)")

asyncio.run(main())
if not _db_existed:
    shutil.rmtree(os.path.join(ROOT, "data"), ignore_errors=True)
