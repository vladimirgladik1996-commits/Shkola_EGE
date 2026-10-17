#!/bin/sh
# Сборка APK кабинета репетитора без Gradle: javac + d8 + aapt2 + apksigner.
set -e
cd "$(dirname "$0")"
# Тулчейн сборки: JDK и Android SDK.
# По умолчанию ищем рядом с папкой apk:  ../jdk  и  ../android-sdk.
# Другое расположение — задайте ANDROID_BUILD_ROOT (внутри — jdk/ и android-sdk/).
ROOT="${ANDROID_BUILD_ROOT:-$(cd .. && pwd)}"
export JAVA_HOME="${JAVA_HOME:-$ROOT/jdk}"
export PATH="$JAVA_HOME/bin:$PATH"
BT="$ROOT/android-sdk/build-tools/34.0.0"
JAR="$ROOT/android-sdk/platforms/android-34/android.jar"

# Подпись: ключ лежит РЯДОМ с build.sh, а не внутри build/ — build/ стирается
# на каждой сборке, и новый ключ каждый раз ломает обновление поверх старого APK.
# Свой ключ (release для Google Play) и пароль — через ANDROID_KEYSTORE / KEYSTORE_PASS.
# Пароль ищем строго в этом порядке: KEYSTORE_PASS (окружение) → файл keystore.pass
# (строка KEYSTORE_PASS=..., тот же каталог; в .gitignore). Дефолтного пароля НЕТ —
# он не должен жить в открытом репозитории.
KS="${ANDROID_KEYSTORE:-keystore.jks}"
KSP="${KEYSTORE_PASS:-}"
if [ -z "$KSP" ] && [ -f keystore.pass ]; then
  KSP="$(sed -n 's/^KEYSTORE_PASS=//p' keystore.pass | head -1)"
fi

# versionCode растёт с каждой сборкой; файл version-code хранится в git,
# чтобы номер не «откатился» на другом компьютере или после переклона.
VC_FILE="version-code"
VC="${VERSION_CODE:-$(cat "$VC_FILE" 2>/dev/null || echo 1)}"
case "$VC" in ''|*[!0-9]*) VC=1 ;; esac

echo "==> javac"
rm -rf build && mkdir -p build/classes build/dex
javac --release 11 -encoding UTF-8 -classpath "$JAR" -d build/classes $(find src -name "*.java")

echo "==> d8"
"$BT/d8" --lib "$JAR" --release --output build/dex build/classes/ru/kabinet/tutor/*.class

echo "==> aapt2 compile (ресурсы: иконка)"
"$BT/aapt2" compile --dir res -o build/res.zip

echo "==> aapt2 link"
"$BT/aapt2" link -I "$JAR" --manifest AndroidManifest.xml -R build/res.zip \
  --min-sdk-version 24 --target-sdk-version 34 --version-code "$VC" --version-name "1.0.$VC" -o build/base.apk

echo "==> dex в apk"
cp build/base.apk build/unsigned.apk
python3 - <<'PY'
import zipfile, shutil, os
shutil.copyfile("build/unsigned.apk", "build/with-dex.apk")
with zipfile.ZipFile("build/with-dex.apk", "a", zipfile.ZIP_DEFLATED) as z:
    z.write("build/dex/classes.dex", "classes.dex")
PY

echo "==> zipalign"
"$BT/zipalign" -f -p 4 build/with-dex.apk build/aligned.apk

echo "==> keystore: $KS"
if [ -f "$KS" ] && [ -z "$KSP" ]; then
  echo "ОШИБКА: ключ $KS уже есть, а пароль не задан." >&2
  echo "  Задайте KEYSTORE_PASS в окружении или создайте keystore.pass со строкой KEYSTORE_PASS=..." >&2
  echo "  Новый ключ НЕ создаётся: иначе обновления поверх старого APK сломаются." >&2
  exit 1
fi
if [ ! -f "$KS" ]; then
  if [ -z "$KSP" ]; then
    # первый запуск: пароль генерируется и сохраняется рядом (не коммитить!)
    KSP="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 28)"
    (umask 077 && printf 'KEYSTORE_PASS=%s\n' "$KSP" > keystore.pass)
    echo "    пароль сгенерирован и записан в keystore.pass (не коммитить) — сделайте БЭКАП: $KS + keystore.pass"
  fi
  keytool -genkeypair -keystore "$KS" -alias tutor -keyalg RSA -keysize 2048 \
    -validity 10000 -storepass "$KSP" -keypass "$KSP" \
    -dname "CN=Kabinet Repetitora, OU=App, O=Local, C=RU" >/dev/null 2>&1
  echo "    создан новый ключ — сохраните $KS: без него обновления поверх старого APK невозможны"
fi

echo "==> apksigner"
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:"$KSP" --key-pass pass:"$KSP" \
  --out kabinet-repetitora.apk build/aligned.apk
"$BT/apksigner" verify --print-certs kabinet-repetitora.apk | head -3

# копия релиза — с номером версии, чтобы новая сборка не затирала прошлые APK.
# каталог releases/ коммитится в git: всегда можно откатиться на прошлую версию.
mkdir -p releases
cp kabinet-repetitora.apk "releases/kabinet-repetitora-$VC.apk"
cp kabinet-repetitora.apk.idsig "releases/kabinet-repetitora-$VC.apk.idsig" 2>/dev/null || true

echo "$((VC + 1))" > "$VC_FILE"
ls -la kabinet-repetitora.apk
echo "BUILD OK (versionCode $VC) → releases/kabinet-repetitora-$VC.apk"
