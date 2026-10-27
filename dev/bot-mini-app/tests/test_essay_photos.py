"""«ИИ-проверка»: фото уходят на сервер СРАЗУ после съёмки — «Готово» отвечает мгновенно
(баг-репорт 007). Раньше все фото (до 12 штук, несколько МБ base64) уходили ОДНИМ тяжёлым
запросом прямо по «Готово»: минуты «долгой загрузки» без признаков жизни, а fetch без таймаута
мог зависнуть навсегда — экран и кнопка «Готово» висли вечно.

Проверяем:
  • фронт: загрузка по одному после съёмки (ecUpKick), сторож тишины с прогрессом отправки
    (ecUpApi), «Готово» собирает сеанс по id мгновенно, сбой/перезапуск сервиса — честная
    догрузка и понятный тост, а не зависание; валидация фото задания/работы сохранена;
  • живой прогон ecUpApi в node: мёртвая сеть обрывается за EC_UP_IDLE (а не висит вечно),
    медленная, но живая сеть (есть прогресс) НЕ убивается;
  • бэкенд: /api/essay_check/photo кладёт фото в копилку сеанса, /api/essay_check/upload
    собирает сеанс из id (порядок сохраняется), чужое/протухшее → 400, старый формат
    «всё сразу одним запросом» работает как раньше (прежние сборки APK).
Запуск: python3 tests/test_essay_photos.py"""
import asyncio, base64, hashlib, hmac, io, json, os, shutil, sqlite3, subprocess, sys, time
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
_db_existed = os.path.exists(os.path.join(ROOT, "data", "tutor.db"))
import auth, bot, chatfiles
from aiohttp.test_utils import TestClient, TestServer

def ok(c): print("  ✓", c)

html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()

mem = sqlite3.connect(":memory:", check_same_thread=False)
bot.init_db(mem)
bot.db = mem; auth.init(mem); auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_pins values('hungerrr', 77)")   # второй пользователь — проверка «чужих» фото
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp()))
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(77,?,?)", (time.time() + 99999, auth._fp())); mem.commit()

def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)

H = {"X-Init": init(42, "Gladik_N")}
H77 = {"X-Init": init(77, "Hungerrr")}

from PIL import Image
def jpg(color=(255, 255, 255)):
    im = Image.new("RGB", (32, 32), color)
    out = io.BytesIO(); im.save(out, "JPEG", quality=85)
    return base64.b64encode(out.getvalue()).decode(), out.getvalue()

JPG1, RAW1 = jpg((255, 255, 255))
JPG2, RAW2 = jpg((200, 200, 200))

# ---------- 1. контракт фронта: фото уходят сразу после съёмки, «Готово» — мгновенно ----------
assert "/api/essay_check/photo" in html, "нет загрузки фото по одному (/api/essay_check/photo)"
assert "const EC_UP_IDLE=30000,EC_UP_MAX=90000" in html, "у загрузки фото нет потолков времени (тишина/общий)"
assert "x.upload.onprogress=()=>arm()" in html, \
    "сторож тишины должен перезапускаться прогрессом отправки — иначе медленную сеть убьёт, а мёртвую не оборвёт"
assert "idle=setTimeout(()=>{try{x.abort()}catch(e){}fin(null)},EC_UP_IDLE)" in html, \
    "мёртвая сеть должна обрываться по тишине (abort), а не висеть вечно"
assert "ecThUpd(true);ecUpKick()}" in html, "после съёмки фото должно уходить на сервер сразу (ecCamShot → ecUpKick)"
assert "ecThUpd();ecUpKick()}" in html, "после выбора из галереи фото должно уходить сразу (ecPicked → ecUpKick)"
assert "{src:await chJpeg(f),set:ecSet,id:null,st:\"wait\"}" in html, \
    "фото должно помнить состояние загрузки (src/set/id/st), а не быть строкой"
assert "sid:ecUpId,w:ecShots.w.map(p=>p.id),t:ecShots.t.map(p=>p.id)" in html, \
    "«Готово» обязан собирать сеанс из уже загруженных id, а не слать base64 заново"
assert "p.st=\"wait\";p.id=null" in html, "после потери фото сервисом (перезапуск) нужна честная перезагрузка"
assert "const pack=a=>a.map" not in html, "тяжёлый запрос «всё сразу по Готово» должен быть убран с фронта"
up_body = html[html.index("async function ecUpload(skip)"):html.index("function ecLogHtml(")]
assert 'if(!ecShots.w.length){ecAskWork();return}' in up_body and \
       'if(!skip&&!ecShots.t.length){ecAskTask();return}' in up_body, \
    "«Готово» обязан проверять фото работы и задания (окна ecAskWork/ecAskTask) — как раньше"
assert "render();scrollTo(0,0);ecRun();" in up_body, "после сборки сеанса переход и старт проверки — сразу"
assert "opt&&opt.idle>0" in html and "opt&&opt.silent" in html, \
    "api() должна уметь прерывать зависший запрос (opt.idle) и молчать по просьбе вызывающего (opt.silent)"
ok("фронт: фото уходят сразу после съёмки, «Готово» собирает сеанс по id мгновенно; валидация задания/работы сохранена")

# ---------- 2. живое поведение ecUpApi: мёртвая сеть обрывается, живая (с прогрессом) — нет ----------
if not shutil.which("node"):
    print("  - node не найден — живой прогон ecUpApi честно пропущен")
else:
    src = html[html.index("function ecUpApi("):html.index("async function ecUpOne(")]
    harness = """
let APP=false, API_BASE="", tg={initData:"x"};
function lockScreen(){throw new Error("lockScreen не должен вызываться")}
const EC_UP_IDLE=150, EC_UP_MAX=400;
let mode="stall", aborted=0, sent=[];
class XHR{                                        /* управляемая заглушка: режим задаётся в mode */
  constructor(){this.upload={};this.h={};this.status=0;this.responseText=""}
  open(m,u){this.m=m;this.u=u}
  setRequestHeader(k,v){this.h[k]=v}
  abort(){aborted++;if(this.onabort)this.onabort()}
  send(b){sent.push(JSON.parse(b));const s=this;
    if(mode=="stall")return;                      /* мёртвая сеть: ни байта, ни ответа — как зависший TCP */
    if(mode=="slow"){let n=0;const t=setInterval(()=>{n+=2;s.upload.onprogress&&s.upload.onprogress({loaded:n,total:10});
      if(n>=10){clearInterval(t);s.status=200;s.responseText='{"id":"p1"}';s.onload&&s.onload()}},40);return}
    if(mode=="http500"){s.status=500;s.responseText="";s.onload&&s.onload();return}
    if(mode=="neterr"){s.onerror&&s.onerror();return}
    s.status=200;s.responseText='{"id":"p2"}';s.onload&&s.onload()}}
globalThis.XMLHttpRequest=XHR;
""" + src + """
(async()=>{
  const out={};
  mode="stall";  let t=Date.now(),r=await ecUpApi("/x",{}); out.stallMs=Date.now()-t; out.stallRes=r;
  mode="slow";   t=Date.now(); r=await ecUpApi("/x",{}); out.slowMs=Date.now()-t; out.slowRes=r;
  mode="http500";r=await ecUpApi("/x",{}); out.httpRes=r;
  mode="neterr"; r=await ecUpApi("/x",{}); out.netRes=r;
  out.aborted=aborted;
  console.log("RESULT:"+JSON.stringify(out));
})().catch(e=>{console.log("ERR:"+e);process.exit(1)});
"""
    r = subprocess.run(["node"], input=harness, capture_output=True, text=True)
    assert r.returncode == 0, f"node: {r.stderr[-500:]}"
    res = json.loads([ln for ln in r.stdout.splitlines() if ln.startswith("RESULT:")][0][7:])
    # F-1 (баг-репорт 014, Push 3): контракт ecUpApi изменён с "null при сбое" на {ok,status,body} —
    # иначе вызывающий не различал «нет связи» и «сервер сказал нет»: всё сваливалось в «проверьте интернет».
    # Мигрированы ТОЛЬКО проверки ФОРМЫ ответа; поведенческие стражи ниже (обрыв по тишине,
    # живая сеть с прогрессом не убивается, abort по тишине) сохранены дословно.
    def shaped(v):
        return isinstance(v, dict) and "ok" in v and "status" in v and "body" in v
    assert shaped(res["stallRes"]) and res["stallRes"]["ok"] is False \
           and res["stallRes"]["status"] == 0 and not res["stallRes"]["body"] \
           and res["stallMs"] < 3000, \
        f"мёртвая сеть должна обрываться за EC_UP_IDLE (status=0, body=null), а не висеть: {res}"
    assert shaped(res["slowRes"]) and res["slowRes"]["ok"] is True \
           and res["slowRes"]["body"] == {"id": "p1"} and res["slowMs"] > 150, \
        f"медленная, но живая сеть (есть прогресс) не должна убиваться сторожем: {res}"
    assert shaped(res["httpRes"]) and res["httpRes"]["ok"] is False and res["httpRes"]["status"] == 500, \
        f"ошибка сервера — статус ДОХОДИТ до вызывающего («сервис перегружен»), а не null: {res}"
    assert shaped(res["netRes"]) and res["netRes"]["ok"] is False and res["netRes"]["status"] == 0 \
           and not res["netRes"]["body"], \
        f"обрыв сети — status=0 и пустое тело (ТОЛЬКО он даёт «нет интернет»): {res}"
    assert res["aborted"] >= 1, f"по тишине запрос должен прерываться (abort): {res}"
    ok("ecUpApi: мёртвая сеть обрывается за EC_UP_IDLE, живая (с прогрессом) доезжает, "
       "сбой даёт {ok,status,body} (F-1) и abort по тишине")

# ---------- 3. бэкенд: копилка фото и мгновенная сборка сеанса по id ----------
async def main():
    real_photo, real_upload = bot.EC_PHOTOS, bot.EC_CHECKS
    async with TestClient(TestServer(bot.make_app())) as c:
        # --- 3а. одно фото после съёмки ---
        assert (await c.post("/api/essay_check/photo", json={"sid": "sess-000001", "set": "w", "d": JPG1}, headers=H)).status == 200
        for body in ({"sid": "short", "set": "w", "d": JPG1},              # кривой sid
                     {"sid": "sess-000002", "set": "x", "d": JPG1},        # кривой набор
                     {"sid": "sess-000003", "set": "w", "d": "не base64!"}, # мусор вместо картинки
                     {"sid": "sess-000004", "set": "w", "d": base64.b64encode(b"not an image").decode()}):
            r = await c.post("/api/essay_check/photo", json=body, headers=H)
            assert r.status == 400, (body, r.status)
        ok("photo: кривой sid/набор/картинка → 400")

        r = await c.post("/api/essay_check/photo", json={"sid": "sess-000005", "set": "w", "n": "1.jpg", "d": JPG1, "mime": "image/jpeg"}, headers=H)
        p1 = (await r.json())["id"]
        r = await c.post("/api/essay_check/photo", json={"sid": "sess-000005", "set": "w", "d": JPG2}, headers=H)
        p2 = (await r.json())["id"]
        r = await c.post("/api/essay_check/photo", json={"sid": "sess-000005", "set": "t", "d": JPG1}, headers=H)
        p3 = (await r.json())["id"]
        assert bot.EC_PHOTOS["sess-000005"][p1]["jpeg"] == chatfiles.to_jpeg_b64(RAW1), "фото должно нормализоваться в JPEG ≤1600 px"
        assert bot.EC_PHOTOS["sess-000005"][p1]["uid"] == 42, "у фото должен храниться владелец"
        ok("photo: три фото сложились в копилку сеанса (JPEG-нормализация, владелец записан)")

        # --- 3б. «Готово»: сборка сеанса по id — лёгкий запрос, порядок сохраняется ---
        r = await c.post("/api/essay_check/upload", json={"sid": "sess-000005", "w": [p2, p1], "t": [p3]}, headers=H)
        d = await r.json()
        assert r.status == 200 and d["n_w"] == 2 and d["n_t"] == 1 and d["id"], (r.status, d)
        uid, out, _ = bot.EC_CHECKS[d["id"]]
        assert uid == 42 and out["w"] == [chatfiles.to_jpeg_b64(RAW2), chatfiles.to_jpeg_b64(RAW1)], "порядок фото обязан сохраняться"
        assert out["t"] == [chatfiles.to_jpeg_b64(RAW1)]
        assert "sess-000005" not in bot.EC_PHOTOS, "собранная копилка должна очищаться"
        ok("upload (id): сеанс собран по id мгновенно, порядок w/t сохранён, копилка очищена")

        # --- 3в. протухшее/чужое/неизвестное — честный 400, клиент перезальёт ---
        bot.EC_PHOTOS["sess-000006"] = {"pid-x": {"uid": 77, "set": "w", "jpeg": "x", "ts": time.time()}}
        r = await c.post("/api/essay_check/upload", json={"sid": "sess-000006", "w": ["pid-x"], "t": []}, headers=H)
        assert r.status == 400 and (await r.json())["error"] == "gone", "чужое фото не должно попадать в чужой сеанс"
        r = await c.post("/api/essay_check/upload", json={"sid": "sess-000007", "w": ["nope"], "t": []}, headers=H)
        assert r.status == 400 and (await r.json())["error"] == "gone", "неизвестный id → gone (сервис перезапустился)"
        bot.EC_PHOTOS["sess-000008"] = {"pid-y": {"uid": 77, "set": "w", "jpeg": "x", "ts": time.time()}}
        r = await c.post("/api/essay_check/upload", json={"sid": "sess-000008", "w": ["pid-y"], "t": []}, headers=H77)
        assert r.status == 200, "владелец должен собирать свои фото"
        ok("upload (id): чужое/протухшее/неизвестное → 400 gone (клиент честно перезальёт), свои — работают")

        # --- 3г. прежний формат «всё сразу одним запросом» (прежние сборки APK) — как раньше ---
        for body in ({"w": [], "t": []}, {"w": [{"d": "не base64!"}], "t": []}):
            assert (await c.post("/api/essay_check/upload", json=body, headers=H)).status == 400
        r = await c.post("/api/essay_check/upload",
                         json={"w": [{"n": "1.jpg", "d": JPG1, "mime": "image/jpeg"}], "t": [{"d": JPG2}]}, headers=H)
        d2 = await r.json()
        assert r.status == 200 and d2["n_w"] == 1 and d2["n_t"] == 1, (r.status, d2)
        assert (await c.post("/api/essay_check/upload", json={"w": [{"d": JPG1}] * 13, "t": []}, headers=H)).status == 400
        ok("upload (старый формат): работает как раньше — прежние сборки APK не ломаются")

        # --- 3д. лимит копилки ---
        sid = "sess-000009"
        for i in range(bot.EC_MAX_PHOTOS):
            r = await c.post("/api/essay_check/photo", json={"sid": sid, "set": "w", "d": JPG1}, headers=H)
            assert r.status == 200
        assert (await c.post("/api/essay_check/photo", json={"sid": sid, "set": "w", "d": JPG1}, headers=H)).status == 400
        ok(f"photo: не больше {bot.EC_MAX_PHOTOS} фото в копилке сеанса → 400")

    bot.EC_PHOTOS, bot.EC_CHECKS = real_photo, real_upload
    print("test_essay_photos: OK")

asyncio.run(main())
if not _db_existed:
    shutil.rmtree(os.path.join(ROOT, "data"), ignore_errors=True)
