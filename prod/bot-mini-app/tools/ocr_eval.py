"""Замер качества распознавания рукописи на ВАШИХ фото: до и после патча.

Зачем: «станет ли лучше» зависит от реального почерка и фото. Скрипт гоняет одни и те же фото через три
пайплайна и считает долю ошибочных слов (WER) и символов (CER) относительно эталона.

  old     — как было: фото ужато до 2600 px (q=.9) → Yandex handwritten → склейка переносов с потерей дефиса
  new     — как стало: фото почти как есть (≤19 Мп, ≤7,5 МБ) → Yandex handwritten → склейка с сохранением дефиса
  verify  — new + второй прогон Yandex (page) + проверка спорных слов через Kimi (нужен KIMI_API_KEY)

Данные: папка DIR, в ней пары  имя.jpg  (оригинал фото страницы, лучше прямо с телефона, без пережатия)
и имя.txt (что ученик ВАМ написал, сплошным текстом; перенос слов через дефис в конце строки — склеить).

Запуск:  YANDEX_API_KEY=... [KIMI_API_KEY=...] python3 tools/ocr_eval.py DIR [--variants old,new,verify]
Метрики считаются без регистра, ё=е, без пунктуации (дефис внутри слова — часть слова)."""
import argparse, asyncio, base64, io, os, re, sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chatfiles, essaycheck, kimi, ocrverify, yavision

# те же пределы, что у бота (bot.py: EC_MAX_SIDE / EC_MAX_PIXELS / EC_MAX_BYTES) — сверяет tests/test_ocr_eval.py
EC_MAX_SIDE, EC_MAX_PIXELS, EC_MAX_BYTES = 6000, 19_000_000, 7_500_000
_TOK = re.compile(r"[0-9a-zа-я]+(?:-[0-9a-zа-я]+)*")


def tokens(text):
    return _TOK.findall((text or "").lower().replace("ё", "е"))


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def wer(truth, got):
    t = tokens(truth)
    return edit_distance(t, tokens(got)) / max(1, len(t))


def cer(truth, got):
    t, g = " ".join(tokens(truth)), " ".join(tokens(got))
    return edit_distance(t, g) / max(1, len(t))


def _old_input(raw):
    """Что получал Яндекс раньше: клиентский canvas ужимал до 2600 px и пережимал q=.9."""
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    if max(im.size) > 2600:
        k = 2600 / max(im.size)
        im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90)
    return base64.b64encode(b.getvalue()).decode()


@contextmanager
def _old_reflow():
    """Старая склейка переносов: дефис терялся всегда."""
    orig = essaycheck._keep_hyphen
    essaycheck._keep_hyphen = lambda cur, nxt: False
    try:
        yield
    finally:
        essaycheck._keep_hyphen = orig


def _essay(pages):
    return essaycheck._reflow("\n\n".join((p.get("text") or "") for p in pages).strip())


async def run_variant(name, raw):
    """→ (итоговый текст, список замен Кими [{from,to}])."""
    if name == "old":
        pages = await yavision.recognize_many([_old_input(raw)])
        with _old_reflow():
            return _essay(pages), []
    img = await asyncio.to_thread(chatfiles.to_jpeg_b64, raw, EC_MAX_SIDE, EC_MAX_PIXELS, EC_MAX_BYTES)
    pages = await yavision.recognize_many([img])
    fixes = []
    if name == "verify":
        pages2 = await yavision.recognize_many([img], model="page")
        doubts = ocrverify.compare(pages, pages2)
        if doubts:
            await asyncio.wait_for(ocrverify.resolve([img], pages, doubts), ocrverify.timeout())
        fixes = [f for p in pages for f in (p.get("vfix") or [])]
    return _essay(pages), fixes


async def evaluate(items, variants):
    """items: [(имя, raw-байты, эталон)] → {вариант: [(имя, wer, cer, текст, замены)]}."""
    out = {v: [] for v in variants}
    for name, raw, truth in items:
        for v in variants:
            text, fixes = await run_variant(v, raw)
            out[v].append((name, wer(truth, text), cer(truth, text), text, fixes))
    return out


def report(res):
    lines = []
    names = [r[0] for r in next(iter(res.values()))] if res else []
    lines.append("%-24s" % "фото" + "".join("%-22s" % (v + " WER / CER") for v in res))
    for k, n in enumerate(names):
        lines.append("%-24s" % n[:23] + "".join("%-22s" % ("%.1f%% / %.1f%%" % (res[v][k][1] * 100, res[v][k][2] * 100)) for v in res))
    if names:
        lines.append("%-24s" % "СРЕДНЕЕ" + "".join("%-22s" % ("%.1f%% / %.1f%%" % (
            100 * sum(r[1] for r in res[v]) / len(res[v]), 100 * sum(r[2] for r in res[v]) / len(res[v]))) for v in res))
    for v in res:
        fx = [(r[0], f) for r in res[v] for f in r[4]]
        if fx:
            lines.append("\nЗамены Кими (%s): %d" % (v, len(fx)))
            lines += ["  %s: %s → %s" % (n, f["from"], f["to"]) for n, f in fx[:40]]
    return "\n".join(lines)


def load(dirpath):
    items = []
    for fn in sorted(os.listdir(dirpath)):
        stem, ext = os.path.splitext(fn)
        tp = os.path.join(dirpath, stem + ".txt")
        if ext.lower() in (".jpg", ".jpeg", ".png") and os.path.exists(tp):
            items.append((stem, open(os.path.join(dirpath, fn), "rb").read(), open(tp, encoding="utf-8").read()))
    return items


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir")
    ap.add_argument("--variants", default="old,new,verify")
    a = ap.parse_args()
    variants = [v for v in a.variants.split(",") if v in ("old", "new", "verify")]
    if not yavision.API_KEY:
        sys.exit("Нужен YANDEX_API_KEY")
    if "verify" in variants and not kimi.API_KEY:
        print("KIMI_API_KEY не задан — вариант verify пропущен")
        variants.remove("verify")
    items = load(a.dir)
    if not items:
        sys.exit("В %s нет пар имя.jpg + имя.txt" % a.dir)
    print(report(asyncio.run(evaluate(items, variants))))


if __name__ == "__main__":
    main()
