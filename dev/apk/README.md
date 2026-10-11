# APK (DEV) — Android-приложение «Надежда DEV»

WebView-обёртка того же кабинета, что и мини-апп. Отличия этой (dev) сборки:

- подсказка по умолчанию — dev-адрес `https://tutor-bot-dev.onrender.com/`
  (адрес можно сменить в приложении: долгое нажатие → «Сменить адрес сервера»);
- имя приложения — «Надежда DEV», `versionName` — `1.0-dev`;
- подписывается debug-ключом, который `build.sh` создаёт автоматически.

## Сборка

Нужны JDK 17+ и Android SDK (build-tools 34.0.0, platform android-34).
Тулчейн ищется рядом с этой папкой: `../jdk` и `../android-sdk`.
Другое расположение:

```bash
export ANDROID_BUILD_ROOT=/путь/к/каталогу   # внутри — jdk/ и android-sdk/
sh build.sh                                   # на выходе: kabinet-repetitora.apk
```

Готовый APK появится в этой папке. Прод-версия приложения (с рабочим
адресом и release-подписью) — в `../../prod/apk/`.
