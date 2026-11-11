"""«ИИ-проверка»: съёмка камерой отдаёт полноразмерный снимок (ImageCapture.takePhoto), а при отсутствии API/сбое —
кадр из превью. Код ecCamPhoto/ecCamGrab/ecCamShot берётся из docs/index.html как есть и гоняется в node
с подставными объектами (DOM не нужен). Без node тест пропускается. Запуск: python3 tests/test_cam_photo_js.py"""
import json, os, re, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def ok(c): print("  ✓", c)

if not shutil.which("node"):
    print("test_cam_photo_js: node не найден — пропуск"); sys.exit(0)

html = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
a = html.index("var ecShotBusy=false;")
end = ".finally(()=>{ecShotBusy=false})}"
b = html.index(end, a) + len(end)
SRC = html[a:b]
assert "ImageCapture" in SRC and "takePhoto" in SRC and "chJpeg(b,6000,.95)" in SRC

# --- непрерывный автофокус при открытии потока ---
assert 'focusMode:"continuous"' in html and "indexOf(\"continuous\")>=0" in html
ok("camTake: запрашивается непрерывный автофокус (если камера его умеет)")

HARNESS = r"""
const SRC = %s;
const results = [];
async function scenario(name, setup, act) {
  const log = {pushed: [], toasts: [], jpegCalls: [], canvasDraws: 0};
  const g = {};
  g.$ = () => ({videoWidth: setup.videoWidth === undefined ? 4000 : setup.videoWidth});
  g.toast = m => log.toasts.push(m);
  g.ecCnt = () => setup.count || 0; g.EC_MAX = 12; g.ecSet = "w"; g.ecShots = {w: [], t: []};
  g.ecThUpd = () => {}; g.ecUpKick = () => {};
  g.chJpeg = async (blob, mx, q) => { log.jpegCalls.push([blob.size, mx, q]); if (setup.jpegFail) throw new Error("x"); return "data:image/jpeg;base64,FULL"; };
  g.camHub = setup.noTrack ? null : {stream: {getVideoTracks: () => [{readyState: "live"}]}};
  g.document = {createElement: () => ({getContext: () => ({drawImage: () => { log.canvasDraws++; }}), toDataURL: () => "data:image/jpeg;base64,FRAME"})};
  if (setup.imageCapture) g.ImageCapture = setup.imageCapture;
  g.setTimeout = (fn, ms) => global.setTimeout(fn, setup.fastTimeout ? 5 : ms);
  const names = Object.keys(g);
  const f = new Function(...names, SRC + "; return {ecCamShot, ecShotBusyGet: () => ecShotBusy};");
  const api = f(...names.map(n => g[n]));
  g.ecShots; // keep ref
  await act(api, g, log);
  log.pushed = g.ecShots.w.map(x => x.src);
  results.push({name, ...log});
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const BIG = {size: 3000000};
  const IC = (fn) => function() { this.takePhoto = fn; };
  await scenario("takePhoto", {imageCapture: IC(async () => BIG)}, async (api) => { api.ecCamShot(); await sleep(50); });
  await scenario("no-api", {}, async (api) => { api.ecCamShot(); await sleep(50); });
  await scenario("throws", {imageCapture: IC(async () => { throw new Error("NotSupported"); })}, async (api) => { api.ecCamShot(); await sleep(50); });
  await scenario("tiny-blob", {imageCapture: IC(async () => ({size: 100}))}, async (api) => { api.ecCamShot(); await sleep(50); });
  await scenario("timeout", {fastTimeout: true, imageCapture: IC(() => new Promise(() => {}))}, async (api) => { api.ecCamShot(); await sleep(120); });
  await scenario("jpeg-fail", {jpegFail: true, imageCapture: IC(async () => BIG)}, async (api) => { api.ecCamShot(); await sleep(50); });
  await scenario("double-tap", {imageCapture: IC(async () => { await sleep(20); return BIG; })}, async (api) => { api.ecCamShot(); api.ecCamShot(); api.ecCamShot(); await sleep(100); });
  await scenario("busy-reset", {imageCapture: IC(async () => BIG)}, async (api, g, log) => { api.ecCamShot(); await sleep(50); api.ecCamShot(); await sleep(50); });
  await scenario("limit", {count: 12, imageCapture: IC(async () => BIG)}, async (api) => { api.ecCamShot(); await sleep(50); });
  await scenario("not-ready", {videoWidth: 0, imageCapture: IC(async () => BIG)}, async (api) => { api.ecCamShot(); await sleep(50); });
  await scenario("no-track", {noTrack: true, imageCapture: IC(async () => BIG)}, async (api) => { api.ecCamShot(); await sleep(50); });
  console.log(JSON.stringify(results));
})();
""" % json.dumps(SRC)

with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
    f.write(HARNESS); path = f.name
r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
os.unlink(path)
assert r.returncode == 0, r.stderr[-800:]
R = {x["name"]: x for x in json.loads(r.stdout.strip().splitlines()[-1])}

x = R["takePhoto"]
assert x["pushed"] == ["data:image/jpeg;base64,FULL"] and x["jpegCalls"] == [[3000000, 6000, 0.95]] and x["canvasDraws"] == 0, x
ok("takePhoto доступен → в проверку уходит полноразмерный снимок (≤6000 px, q=.95), кадр из превью не нужен")
for n in ("no-api", "throws", "tiny-blob", "timeout", "jpeg-fail", "no-track"):
    x = R[n]
    assert x["pushed"] == ["data:image/jpeg;base64,FRAME"] and x["canvasDraws"] == 1, (n, x)
ok("нет ImageCapture / сбой / пустой снимок / таймаут / ошибка кодирования / нет потока → запасной кадр из превью")
x = R["double-tap"]
assert x["pushed"] == ["data:image/jpeg;base64,FULL"], x
ok("тройное нажатие затвора во время съёмки → одно фото, а не три")
assert len(R["busy-reset"]["pushed"]) == 2
ok("после снимка затвор снова доступен")
assert R["limit"]["pushed"] == [] and any("Не больше" in t for t in R["limit"]["toasts"])
assert R["not-ready"]["pushed"] == [] and any("не готова" in t for t in R["not-ready"]["toasts"])
ok("лимит фото и «камера не готова» обрабатываются как раньше")
print("test_cam_photo_js: все проверки прошли")
