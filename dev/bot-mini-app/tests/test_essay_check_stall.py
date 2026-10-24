"""«ИИ-проверка» зависала: обработка начиналась, но не доводилась до конца и висела вечно.

Было три независимых обрыва, сливавшихся в одно «зависло навсегда»:
  • стрим МОЛЧАЛ минутами (OCR до 12 фото, анализ Кими, повтор разбора) → reverse-proxy
    (Render) и WebView тихо рвали соединение посреди проверки;
  • фронт: конец стрима без done/fail/off НЕ считался сбоем (охранник требовал «ответ вообще
    без событий»), а fetch/read() жили без таймаута — мёртвая сеть вешала промис навсегда;
  • бэкенд: у проверки не было ни пульса, ни потолка времени — вечный стрим был возможен.
Стало: пульс {"t":"hb"} в тишине (EC_BEAT), потолок EC_TIMEOUT → {"t":"fail","why":"slow"},
конец стрима без итога → {"t":"fail","why":"net"}, у клиента — потолок тишины EC_IDLE,
по которому apiStream закрывает поток, а ecRun честно показывает сбой + «Повторить».
Запуск: python3 tests/test_essay_check_stall.py"""
import asyncio, hashlib, hmac, json, os, shutil, sqlite3, subprocess, sys, time
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BOT_TOKEN="123456:TESTTOKEN", PUBLIC_URL="https://bot.example.com", LOG_LEVEL="CRITICAL")
for k in ("ALLOWED_USERNAMES", "ALLOWED_IDS", "OWNER_ID"): os.environ.pop(k, None)
sys.path.insert(0, ROOT)
_db_existed = os.path.exists(os.path.join(ROOT, "data", "tutor.db"))
import auth, bot, essaycheck, kimi, yavision
from aiohttp.test_utils import TestClient, TestServer

def ok(c): print("  ✓", c)

html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()

mem = sqlite3.connect(":memory:", check_same_thread=False)
bot.init_db(mem)
bot.db = mem; auth.init(mem); auth.PW_HASH = auth.make_hash("Tst-Only-Pw-7qZ4!")
mem.execute("insert into auth_pins values('gladik_n', 42)")
mem.execute("insert into auth_sessions(tg_id,exp,fp) values(42,?,?)", (time.time() + 99999, auth._fp())); mem.commit()

def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    d["hash"] = hmac.new(hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest(), chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(d)

H = {"X-Init": init(42, "Gladik_N")}
kimi.enabled = lambda: True
yavision.enabled = lambda: True

def ndjson(raw):
    assert b"HTTP/1.1" not in raw, "в теле стрима второй HTTP-статус: %r" % raw[:120]
    return [json.loads(ln) for ln in raw.decode("utf-8").splitlines() if ln.strip()]

# ---------- 1. контракт фронта: потолок тишины и честный итог стрима ----------
assert "const EC_IDLE=" in html, "нет потолка тишины стрима проверки (EC_IDLE)"
assert "const wait=p=>idle?Promise.race([p,deadP.then(()=>null)]):p" in html, \
    "apiStream не прерывается по тишине — зависший fetch/read() висит вечно"
assert "if(rd)rd.cancel()" in html, "по тишине поток надо закрывать (rd.cancel)"
assert 'apiStream("/api/essay_check/run",{id:ecSess,kind:ecKind},on,{idle:EC_IDLE})' in html, \
    "ecRun должен передавать потолок тишины в apiStream"
run_body = html[html.index("async function ecRun(){"):html.index("function ecRetry(")]
assert 'if(id===ecRunId&&ecState=="run"&&!locked)ecFail("net")' in run_body, \
    "конец стрима без done/fail/off обязан быть сбоем — раньше экран висел вечно"
assert "!got" not in run_body, "охранник снова требует «пустой ответ»: события без итога вешали экран"
assert 'ecWhy=(why==="empty"||why==="http"||why==="slow")?why:"net"' in html, \
    "ecFail должен различать причину slow"
assert 'ecWhy==="slow"' in html, "для «slow» нужен свой текст ошибки в ecWhyMsg()"
ok("фронт: потолок тишины EC_IDLE, конец стрима без итога = сбой (без условия «пустой ответ»)")

# ---------- 2. живое поведение apiStream: мёртвая сеть больше не вешает проверку ----------
if not shutil.which("node"):
    print("  - node не найден — живой прогон apiStream честно пропущен")
else:
    src = html[html.index("async function apiStream("):html.index("function ingest(d)")]
    harness = """
let APP=false, API_BASE="", tg={initData:""}, cancelled=0;
function lockScreen(){throw new Error("lockScreen не должен вызываться")}
""" + src + """
function reader(chunks,stall){let i=0,pend=null;   /* stall: после кусков — вечно молчащая сеть (read() не завершается) */
  return {read:()=>i<chunks.length?Promise.resolve({value:new TextEncoder().encode(chunks[i++]),done:false}):stall?new Promise(res=>{pend=res}):Promise.resolve({value:undefined,done:true}),
          cancel:()=>{cancelled++;if(pend){pend({value:undefined,done:true});pend=null}}};}
const asResp=(chunks,stall)=>({status:200,body:{getReader:()=>reader(chunks,stall)}});
(async()=>{
  const out=[];
  globalThis.fetch=()=>new Promise(()=>{});                       /* 1) fetch не отвечает вовсе */
  let t=Date.now();await apiStream("/x",{},e=>out.push(e),{idle:120});const fHang=Date.now()-t;
  const afterFetch=out.length;
  globalThis.fetch=async()=>asResp([],true);                      /* 2) тело молчит вечно */
  t=Date.now();await apiStream("/x",{},e=>out.push(e),{idle:120});const bHang=Date.now()-t;
  const afterBody=out.length;
  globalThis.fetch=async()=>asResp(['{"t":"status","v":"a"}\\n','{"t":"incomplete'],true);
  await apiStream("/x",{},e=>out.push(e),{idle:120});             /* 3) события, потом тишина с недописанной строкой */
  const afterStall=out.length;
  globalThis.fetch=async()=>asResp(['{"t":"a"}\\n','{"t":"b"}']);  /* 4) штатный конец: всё доставлено, включая хвост */
  await apiStream("/x",{},e=>out.push(e),{idle:500});
  console.log("RESULT:"+JSON.stringify({fHang,bHang,cancelled,afterFetch,afterBody,afterStall,out}));
})().catch(e=>{console.log("ERR:"+e);process.exit(1)});
"""
    r = subprocess.run(["node"], input=harness, capture_output=True, text=True)
    assert r.returncode == 0, f"node: {r.stderr[-500:]}"
    res = json.loads([ln for ln in r.stdout.splitlines() if ln.startswith("RESULT:")][0][7:])
    assert res["fHang"] < 3000 and res["bHang"] < 3000, f"fetch/тело висят вечно: {res}"
    assert res["afterFetch"] == 0 and res["afterBody"] == 0, f"на мёртвой сети придумались события: {res}"
    assert res["out"][res["afterBody"]:res["afterStall"]] == [{"t": "status", "v": "a"}], \
        f"недописанная строка не должна доставляться событием: {res}"
    assert res["out"][res["afterStall"]:] == [{"t": "a"}, {"t": "b"}], f"штатный конец стрима сломан: {res}"
    assert res["cancelled"] >= 2, f"поток по тишине не закрывается: {res}"
    ok("apiStream: зависший fetch/тело обрываются по тишине, недописанная строка не доставляется, штатный конец цел")

# ---------- 3-5. бэкенд: пульс в тишине, потолок времени и честный итог ----------
async def main():
    real_run = essaycheck.run
    async with TestClient(TestServer(bot.make_app())) as c:
        # --- 3. тишина в пайплайне: пульс {"t":"hb"} держит соединение, потолок → fail/slow ---
        async def slow_run(*a, **k):
            yield "status", "Собрала фото"
            await asyncio.sleep(5)                     # «вечный» этап: OCR/анализ молчит
            yield
        essaycheck.run, bot.EC_BEAT, bot.EC_TIMEOUT = slow_run, 0.2, 0.5
        bot.EC_CHECKS["cid1"] = [42, {"w": ["x"], "t": []}, time.time()]
        t0 = time.monotonic()
        r = await c.post("/api/essay_check/run", json={"id": "cid1"}, headers=H)
        evs = ndjson(await r.read())
        took = time.monotonic() - t0
        assert r.status == 200 and "ndjson" in r.headers["Content-Type"], (r.status, r.headers)
        assert evs[0] == {"t": "status", "v": "Собрала фото"}, evs
        assert {"t": "hb"} in evs, f"в тишине стрима нет пульса — соединение порвут прокси/WebView: {evs}"
        assert evs[-1] == {"t": "fail", "why": "slow"}, evs
        assert took < 3, f"потолок EC_TIMEOUT не сработал: {took:.1f} с"
        ok(f"тишина пайплайна: пульс {{\"t\":\"hb\"}} каждые {bot.EC_BEAT} с, потолок {bot.EC_TIMEOUT} с → fail/slow за %.1f с" % took)

        # --- 4. пайплайн кончился БЕЗ итога (как раньше «зависало») → fail/net, а не молчание ---
        async def bare_run(*a, **k):
            yield "status", "Распознан рукописный текст"
            return
            yield
        essaycheck.run = bare_run
        bot.EC_CHECKS["cid2"] = [42, {"w": ["x"], "t": []}, time.time()]
        r = await c.post("/api/essay_check/run", json={"id": "cid2"}, headers=H)
        evs = ndjson(await r.read())
        assert evs[-1] == {"t": "fail", "why": "net"}, evs
        ok("пайплайн без итога: стрим закрывается событием fail/net — клиент не виснет")

        # --- 5. штатный конец: done остаётся последним событием, fail не дописывается ---
        async def done_run(*a, **k):
            yield "status", "Сверяю с критериями ФИПИ…"
            yield "done", {"errors": [], "criteria": [], "recommendations": [],
                           "score": {"got": 0, "max": 0, "percent": 0, "level": "Отлично"}}
        essaycheck.run = done_run
        bot.EC_CHECKS["cid3"] = [42, {"w": ["x"], "t": []}, time.time()]
        r = await c.post("/api/essay_check/run", json={"id": "cid3"}, headers=H)
        evs = ndjson(await r.read())
        assert evs[-1]["t"] == "done" and not any(e.get("t") == "fail" for e in evs), evs
        ok("штатный конец: done последним событием, ложного fail нет")
        essaycheck.run = real_run
        bot.EC_BEAT, bot.EC_TIMEOUT = 15.0, 900.0

    print("test_essay_check_stall: OK")

asyncio.run(main())
if not _db_existed:
    shutil.rmtree(os.path.join(ROOT, "data"), ignore_errors=True)
