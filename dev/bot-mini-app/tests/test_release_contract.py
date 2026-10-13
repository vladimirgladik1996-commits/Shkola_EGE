"""Контракт релиза: файлы не «пропадают» при пушах, сборке образа и выпуске APK.

Именно из-за этих вещей файлы раньше терялись:
  • Dockerfile перечислял файлы по одному — новые модули не попадали в образ;
  • ключ подписи APK (keystore.jks) жил только на одной машине и пропал;
  • каждая сборка APK затирала предыдущую.
Здесь — автоматическая проверка, что все защиты на месте.
Запуск: python3 tests/test_release_contract.py
"""
import os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../bot-mini-app
BASE = os.path.normpath(os.path.join(ROOT, ".."))                        # dev/ или prod/
REPO = os.path.normpath(os.path.join(ROOT, "..", ".."))                  # корень репозитория
HERE = os.path.basename(BASE)


def ok(m):
    print("  ✓", m)


def read(path, binary=False):
    if binary:
        with open(path, "rb") as f:
            return f.read()
    with open(path, encoding="utf-8") as f:
        return f.read()


def main():
    # 1. образ собирается целиком — любой новый файл попадает в релиз автоматически
    df = read(os.path.join(ROOT, "Dockerfile"))
    assert "COPY . ." in df, \
        "Dockerfile должен копировать весь каталог (COPY . .) — иначе новые модули не попадут в образ"
    ok("Dockerfile копирует каталог целиком — новые файлы не теряются в релизе")

    # 2. секреты и рабочие данные в образ не попадают
    di = read(os.path.join(ROOT, ".dockerignore"))
    for must in (".env", "data/", "backups/", ".jks", ".pass", "tests/"):
        assert must in di, f".dockerignore должен исключать {must}"
    ok(".dockerignore исключает .env, data/, backups/, ключи подписи и tests/")

    # 3. всё, что импортирует бот, лежит рядом — иначе контейнер падает при старте
    src = read(os.path.join(ROOT, "bot.py"))
    mods = set(re.findall(r"^\s*from\s+([A-Za-z_]\w*)\s+import", src, re.M))
    for line in re.findall(r"^\s*import\s+([A-Za-z0-9_, ]+)", src, re.M):  # import a, b, c
        for name in line.split(","):
            name = name.strip().split(" as ")[0].strip()
            if name:
                mods.add(name)
    local = {m for m in mods if os.path.exists(os.path.join(ROOT, m + ".py"))}
    need = {"assist", "auth", "kimi", "whisper"}
    assert need <= local, f"не хватает модулей рядом с bot.py: {sorted(need - local)}"
    ok("модули на месте: " + ", ".join(sorted(need)))

    # 4. файлы мини-аппа и шрифты
    assert os.path.exists(os.path.join(ROOT, "docs", "index.html")), \
        "нет docs/index.html — мини-апп пропадёт"
    fonts = os.path.join(ROOT, "fonts")
    assert os.path.isdir(fonts) and os.listdir(fonts), \
        "папка fonts/ пуста — PDF-отчёты не соберутся"
    ok("мини-апп (docs/index.html) и шрифты на месте")

    # 5. dev и prod не разъехались: код обязан быть идентичен
    other_name = "prod" if HERE == "dev" else "dev"
    other = os.path.join(REPO, other_name, "bot-mini-app")
    if os.path.isdir(other):
        skip = {".env.example", "DEPLOY.md", "deploy.sh", "__pycache__", "data", "backups", ".pytest_cache"}
        diffs = []
        for a, b, tag in ((ROOT, other, other_name), (other, ROOT, HERE)):
            for dirpath, dirnames, filenames in os.walk(a):
                dirnames[:] = [d for d in dirnames if d not in skip]
                rel = os.path.relpath(dirpath, a)
                for fn in sorted(filenames):
                    if fn in skip:
                        continue
                    p1, p2 = os.path.join(dirpath, fn), os.path.join(b, rel, fn)
                    if not os.path.exists(p2):
                        diffs.append(f"{os.path.join(rel, fn)} — нет в {tag}/")
                    elif read(p1, True) != read(p2, True):
                        diffs.append(f"{os.path.join(rel, fn)} — отличается")
        assert not diffs, "dev и prod разъехались: " + "; ".join(sorted(set(diffs))[:12])
        ok(f"код {HERE}/ и {other_name}/ идентичен (правки переносим в обе копии)")

    # 6. APK: исходники, собранный файл и номер версии — в git; ключ подписи — нет
    apk = os.path.join(BASE, "apk")
    for f in ("AndroidManifest.xml", "build.sh", "version-code", "kabinet-repetitora.apk", "src", "res"):
        assert os.path.exists(os.path.join(apk, f)), f"нет apk/{f} — релиз APK неполный"
    vc = read(os.path.join(apk, "version-code")).strip()
    assert vc.isdigit() and int(vc) > 0, "apk/version-code должен быть положительным числом"
    gi = read(os.path.join(REPO, ".gitignore"))
    for secret in ("keystore.jks", "keystore.pass"):
        assert secret in gi, f"{secret} должен быть в .gitignore — ключ подписи в git не коммитим"
    assert "mkdir -p releases" in read(os.path.join(apk, "build.sh")), \
        "build.sh должен складывать собранные APK в releases/ с номером версии"
    ok(f"APK на месте (version-code {vc}); сборка складывает версии в apk/releases/; ключ подписи — вне git")


if __name__ == "__main__":
    main()
    print("OK")
