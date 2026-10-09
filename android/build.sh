#!/bin/sh
# Сборка APK кабинета репетитора без Gradle: javac + d8 + aapt2 + apksigner.
set -e
cd "$(dirname "$0")"
TMP=../                      # .openclaw/tmp
export JAVA_HOME="$TMP/jdk"
export PATH="$JAVA_HOME/bin:$PATH"
BT="$TMP/android-sdk/build-tools/34.0.0"
JAR="$TMP/android-sdk/platforms/android-34/android.jar"

echo "==> javac"
rm -rf build && mkdir -p build/classes build/dex
javac --release 11 -encoding UTF-8 -classpath "$JAR" -d build/classes src/ru/kabinet/tutor/MainActivity.java

echo "==> d8"
"$BT/d8" --lib "$JAR" --release --output build/dex build/classes/ru/kabinet/tutor/*.class

echo "==> aapt2 link"
"$BT/aapt2" link -I "$JAR" --manifest AndroidManifest.xml \
  --min-sdk-version 24 --target-sdk-version 34 --version-code 1 --version-name "1.0" -o build/base.apk

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

echo "==> keystore"
if [ ! -f build/ks.jks ]; then
  keytool -genkeypair -keystore build/ks.jks -alias tutor -keyalg RSA -keysize 2048 \
    -validity 10000 -storepass tutorpass -keypass tutorpass \
    -dname "CN=Kabinet Repetitora, OU=App, O=Local, C=RU" >/dev/null 2>&1
fi

echo "==> apksigner"
"$BT/apksigner" sign --ks build/ks.jks --ks-pass pass:tutorpass --key-pass pass:tutorpass \
  --out kabinet-repetitora.apk build/aligned.apk
"$BT/apksigner" verify --print-certs kabinet-repetitora.apk | head -3
ls -la kabinet-repetitora.apk
echo "BUILD OK"
