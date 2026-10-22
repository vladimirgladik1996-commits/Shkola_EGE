package ru.kabinet.tutor;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.media.AudioManager;
import android.media.AudioRecordingConfiguration;
import android.media.MediaRecorder;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Looper;
import android.provider.MediaStore;
import android.util.Base64;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.ValueCallback;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.FutureTask;
import java.util.concurrent.TimeUnit;

/** Обёртка кабинета репетитора: WebView поверх того же адреса, что и мини-апп. */
public class MainActivity extends Activity {
    private WebView web;
    private SharedPreferences prefs;
    private ValueCallback<Uri[]> fileCb;            // выбор фото из галереи / файла (см. onShowFileChooser)
    private File captureFile;                       // сюда системная камера кладёт снимок («Снять камерой»)
    private float lpX, lpY;                         // точка последнего касания — для elementFromPoint при долгом тапе
    private static final int FILE_CHOOSER_REQ = 1001;
    private static final int MIC_PERM_REQ = 1002;
    private static final int CAMERA_REQ = 1003;          // «Снять камерой»: снимок возвращается отдельно от выбора файла
    private static final int CAMERA_PERM_REQ = 1004;     // встроенная камера «Снять камерой» (getUserMedia на странице)
    private final List<BroadcastReceiver> shareReceivers = new ArrayList<>();   // ждут окончания загрузки PDF; снимаются в onDestroy
    private MediaRecorder micRec;                   // нативная запись голоса (см. JsBridge.nativeRec)
    private File micFile;
    private long micT0;
    private boolean webPaused;                      // WebView приостановлен (onPause) — не гасить его повторно

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("app", MODE_PRIVATE);
        String url = prefs.getString("url", "");
        if (url.isEmpty()) showSetup(""); else showWeb(url);
    }

    /** Первый запуск (или смена адреса): поле для HTTPS-адреса сервера. */
    private void showSetup(String current) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        float d = getResources().getDisplayMetrics().density;
        int p = (int) (20 * d);
        box.setPadding(p, p * 3, p, p);

        final EditText edit = new EditText(this);
        edit.setHint("https://ваш-адрес.onrender.com/");
        edit.setText(current.isEmpty() ? "https://tutor-bot-dev.onrender.com/" : current);
        edit.setSingleLine(true);

        Button b = new Button(this);
        b.setText("Открыть кабинет");

        box.addView(edit, new LinearLayout.LayoutParams(-1, -2));
        box.addView(b, new LinearLayout.LayoutParams(-1, -2));
        setContentView(box);

        b.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                String u = edit.getText().toString().trim();
                if (!u.startsWith("https://")) {
                    Toast.makeText(MainActivity.this, "Нужен адрес, начинающийся с https://", Toast.LENGTH_LONG).show();
                    return;
                }
                if (!u.endsWith("/")) u += "/";
                prefs.edit().putString("url", u).apply();
                showWeb(u);
            }
        });
    }

    private void showWeb(final String url) {
        web = new WebView(this);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);          // localStorage: токен входа
        s.setDatabaseEnabled(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        // страница всегда с сервера: кэш WebView не должен отдать старый фронт после редеплоя —
        // иначе обновлённый APK работает со старой страницей и её старыми обработчиками
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);

        web.addJavascriptInterface(new JsBridge(), "Android");   // «Поделиться PDF» + микрофон (см. JsBridge)

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                String scheme = u.getScheme(), host = u.getHost(), baseHost = Uri.parse(url).getHost();
                // Вложенные фреймы (материалы во фрейме, скачивание PDF через blob) остаются внутри
                // WebView: их нельзя отдавать наружу — ни один браузер их не откроет, а startActivity
                // без обработчика сворачивает/роняет приложение (так вылетало при открытии материалов).
                if (!r.isForMainFrame()) return false;
                // blob: / data: / about: — служебные ссылки страницы (материалы, скачивание PDF через blob).
                if (scheme == null || "blob".equals(scheme) || "data".equals(scheme) || "about".equals(scheme)) return false;
                if ("https".equals(scheme) && host != null
                        && (host.equals(baseHost) || host.endsWith(".telegram.org") || host.equals("t.me"))) {
                    return false;                       // свой сервер и Telegram — внутри приложения
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));   // всё наружу — системным браузером
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "Не получилось открыть ссылку", Toast.LENGTH_LONG).show();
                }
                return true;
            }

            /** Рендерер WebView умер (нехватка памяти, сбой внутри страницы). Без этого обработчика
             *  Android просто закрывает приложение («сворачивается» на рабочий стол). Возвращаем true
             *  и пересоздаём WebView — пользователь остаётся в приложении. */
            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                runOnUiThread(new Runnable() {
                    public void run() {
                        try { view.destroy(); } catch (Exception ignored) {}
                        if (web == view) web = null;
                        String u = prefs.getString("url", "");
                        if (u.isEmpty()) showSetup(""); else showWeb(u);
                    }
                });
                return true;
            }
        });
        // alert / confirm / prompt из страницы + выбор файла из галереи:
        // без onShowFileChooser WebView молча игнорирует <input type="file">
        // («Фото ученика → Выбрать из галереи» ничего не открывало).
        web.setWebChromeClient(new WebChromeClient() {
            /** Страница просит микрофон и/или камеру (getUserMedia: голосовой помощник и встроенная
             *  съёмка «Снять камерой»). Без этого обработчика WebView молча отклоняет запрос.
             *  Разрешаем только звук/видео и только нашему серверу, после системного запроса. */
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(new Runnable() {
                    public void run() {
                        try {
                            String reqHost = request.getOrigin() == null ? null : request.getOrigin().getHost();
                            String nowUrl = web == null ? null : web.getUrl();
                            String nowHost = nowUrl == null ? null : Uri.parse(nowUrl).getHost();
                            boolean ours = reqHost != null && (reqHost.equals(Uri.parse(url).getHost()) || reqHost.equals(nowHost));
                            boolean audio = false, video = false;
                            for (String r : request.getResources()) {
                                if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)) audio = true;
                                if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) video = true;
                            }
                            if (!ours || (!audio && !video)) { request.deny(); return; }
                            boolean micOk = !audio || checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
                            boolean camOk = !video || checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED;
                            if (micOk && camOk) {
                                List<String> grant = new ArrayList<>();
                                if (audio) grant.add(PermissionRequest.RESOURCE_AUDIO_CAPTURE);
                                if (video) grant.add(PermissionRequest.RESOURCE_VIDEO_CAPTURE);
                                request.grant(grant.toArray(new String[0]));
                                return;
                            }
                            // ВАЖНО: системный диалог поверх pending-запроса WebView отменяет его на части
                            // устройств (onPermissionRequestCanceled), и getUserMedia падает без объяснений —
                            // «Микрофон недоступен». Поэтому запрос страницы закрываем СРАЗУ, разрешение
                            // просим отдельно, а страница повторяет нажатие (см. виджет: micStatus/askMic).
                            if (!micOk) {
                                request.deny();
                                askMicPermission(true);
                                Toast.makeText(MainActivity.this, "Разрешите микрофон — и нажмите кнопку ещё раз", Toast.LENGTH_LONG).show();
                            } else {
                                request.deny();
                                askCameraPermission(true);
                                Toast.makeText(MainActivity.this, "Разрешите камеру — и нажмите кнопку ещё раз", Toast.LENGTH_LONG).show();
                            }
                        } catch (Exception ignored) {
                            // запрос WebView мог быть уже отменён (onPermissionRequestCanceled) — не роняем приложение
                        }
                    }
                });
            }

            @Override
            public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams params) {
                if (fileCb != null) fileCb.onReceiveValue(null);   // прошлый выбор не завершился
                fileCb = cb;
                // capture="environment" у <input> — страница просит снять фото/видео КАМЕРОЙ, а не выбирать
                // из галереи («Снять камерой» → «Прикрепите материал к задаче» в чате ИИ)
                if (params != null && params.isCaptureEnabled() && launchCamera(params)) return true;
                try {
                    Intent i = params != null ? params.createIntent() : new Intent(Intent.ACTION_GET_CONTENT);
                    i.addCategory(Intent.CATEGORY_OPENABLE);
                    if (params != null && params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) {
                        i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);   // несколько файлов за раз (вложения чата)
                    }
                    fixChooserTypes(i, params);
                    startActivityForResult(i, FILE_CHOOSER_REQ);
                } catch (Exception e) {
                    fileCb = null;
                    captureFile = null;
                    Toast.makeText(MainActivity.this, "Не получилось открыть выбор файла", Toast.LENGTH_LONG).show();
                    return false;
                }
                return true;
            }
        });

        web.setDownloadListener(new DownloadListener() {
            public void onDownloadStart(String u, String agent, String disposition, String mime, long size) {
                try {
                    String name = "raspisanie.pdf";
                    int i = disposition == null ? -1 : disposition.indexOf("filename=");
                    if (i >= 0) {
                        name = disposition.substring(i + 9).replace("\"", "").trim();
                        int slash = Math.max(name.lastIndexOf('/'), name.lastIndexOf('\\'));
                        if (slash >= 0) name = name.substring(slash + 1);
                        if (name.isEmpty()) name = "raspisanie.pdf";
                    }
                    DownloadManager.Request req = new DownloadManager.Request(Uri.parse(u));
                    req.setMimeType(mime);
                    req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                    ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(req);
                    Toast.makeText(MainActivity.this, "Файл сохраняется в «Загрузки»", Toast.LENGTH_LONG).show();
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "Не получилось сохранить файл", Toast.LENGTH_LONG).show();
                }
            }
        });

        // долгое нажатие — служебное меню (сменить адрес);
        // страница помечает нажатия на кнопках (window.__lpOnBtn), чтобы меню не выскакивало
        // поверх виджета — раньше долгий тап по «Голосовая команда» открывал «Сменить адрес».
        // Страховка не только на флаг страницы (он есть не во всех версиях фронта): открыта ли
        // шторка (#sheet.on) и что лежит под пальцем (elementFromPoint по точке касания).
        web.setOnTouchListener((v, ev) -> {
            if (ev.getActionMasked() == MotionEvent.ACTION_DOWN) { lpX = ev.getX(); lpY = ev.getY(); }
            return false;   // не перехватываем — касания обрабатывает сам WebView
        });
        web.setOnLongClickListener(v -> {
            float scale = web.getScale() > 0 ? web.getScale() : 1f;
            final String js = "(function(){try{"
                    + "if(window.__lpOnBtn===true)return true;"
                    + "if(document.querySelector('#sheet.on'))return true;"
                    + "var e=document.elementFromPoint(" + ((lpX + web.getScrollX()) / scale) + "," + ((lpY + web.getScrollY()) / scale) + ");"
                    + "return !!(e&&e.closest&&e.closest('button,a,input,select,textarea,[onclick]'));"
                    + "}catch(x){return false}})()";
            web.evaluateJavascript(js, new ValueCallback<String>() {
                public void onReceiveValue(String value) {
                    if ("true".equals(value)) return;   // долгий тап по кнопке/шторке — не служебное меню
                    new AlertDialog.Builder(MainActivity.this)
                            .setTitle("Кабинет репетитора")
                            .setItems(new String[]{"Сменить адрес сервера"}, (dlg, which) -> {
                                prefs.edit().remove("url").apply();
                                showSetup(url);
                            })
                            .setNegativeButton("Отмена", null)
                            .show();
                }
            });
            return true;
        });

        web.loadUrl(url);
        // Разрешения НЕ спрашиваем при открытии приложения — только при первом реальном
        // использовании камеры/микрофона (см. onPermissionRequest, askMic/askCamera).
    }

    /**
     * Запрос микрофона. Пока системный диалог открыт, страница
     * НЕ должна держать pending-запрос getUserMedia — на части устройств такой
     * конфликт отменяет запрос WebView, и запись падает без объяснений.
     * force=false — мягкий запрос, «больше не спрашивать» уважаем (разрешения НЕ просим при открытии — только при первом использовании);
     * force=true — явное нажатие кнопки в виджете (см. JsBridge.askMic).
     */
    private void askMicPermission(boolean force) {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) return;
        if (!force && prefs.getBoolean("micAsked", false)
                && !shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO)) return;
        prefs.edit().putBoolean("micAsked", true).apply();
        requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MIC_PERM_REQ);
    }

    /**
     * Запрос камеры — встроенная съёмка «Снять камерой» (getUserMedia на странице).
     * Та же схема, что у микрофона: pending-запрос WebView не держим, разрешение просим
     * отдельно, страница повторяет нажатие (см. chCam: camStatus/askCamera).
     * Запасной путь «Снять камерой» (системная камера через ACTION_IMAGE_CAPTURE)
     * разрешения не требует и продолжает работать без него.
     */
    private void askCameraPermission(boolean force) {
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) return;
        if (!force && prefs.getBoolean("camAsked", false)
                && !shouldShowRequestPermissionRationale(Manifest.permission.CAMERA)) return;
        prefs.edit().putBoolean("camAsked", true).apply();
        requestPermissions(new String[]{Manifest.permission.CAMERA}, CAMERA_PERM_REQ);
    }

    /**
     * JS-мост для страницы (вызывается как Android.*):
     *  - sharePdf(url, name) — «Поделиться PDF»: скачивание + системный chooser;
     *  - shareFile(url, name, mime) — «Поделиться» файлом из чата ИИ в любом формате (docx/pdf/png):
     *    скачать + системное окно ПЕРЕСЫЛКИ файла (Telegram и другие контакты);
     *  - openFile(url, name, mime) — «Скачать»/тап по файлу: скачать и СРАЗУ ОТКРЫТЬ на экране
     *    системным просмотрщиком (ACTION_VIEW) — кнопки «Скачать» и карточка файла зовут именно его;
     *  - micStatus() — состояние микрофона: granted / denied / prompt — виджет решает,
     *    звать ли getUserMedia или сразу вести пользователя в настройки;
     *  - askMic() — явный системный запрос микрофона (без pending-запроса WebView);
     *  - camStatus() / askCamera() — то же самое для камеры («Снять камерой» — съёмка в приложении);
     *  - openSettings() — настройки приложения: включить микрофон/камеру после «больше не спрашивать».
     * Методы моста вызываются из фонового потока — работу переносим в UI-поток.
     */
    private class JsBridge {
        @JavascriptInterface
        public void sharePdf(final String url, final String name) {
            runOnUiThread(new Runnable() {
                public void run() { startShare(url, name, "application/pdf", 0); }
            });
        }

        /** Файл из «Чата с ИИ» (docx/pdf/png): скачать в «Загрузки» и открыть системное окно пересылки
         *  (Telegram и другие контакты) — для файлов в любом формате, mime не зашит в код.
         *  СТРОГО кнопка «Поделиться»: открывает окно пересылки, а не просмотр документа. */
        @JavascriptInterface
        public void shareFile(final String url, final String name, final String mime) {
            runOnUiThread(new Runnable() {
                public void run() {
                    startDownload(url, name, (mime == null || mime.isEmpty()) ? "application/octet-stream" : mime, 0, false);
                }
            });
        }

        /** «Скачать» и тап по файлу/стикеру: скачать и СРАЗУ ОТКРЫТЬ документ на экране
         *  (DownloadManager + ACTION_VIEW — системный просмотрщик .docx/.pdf/.png). */
        @JavascriptInterface
        public void openFile(final String url, final String name, final String mime) {
            runOnUiThread(new Runnable() {
                public void run() {
                    startDownload(url, name, (mime == null || mime.isEmpty()) ? "application/octet-stream" : mime, 0, true);
                }
            });
        }

        @JavascriptInterface
        public String micStatus() {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) return "granted";
            if (prefs.getBoolean("micAsked", false)
                    && !shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO)) return "denied";
            return "prompt";
        }

        @JavascriptInterface
        public void askMic() {
            runOnUiThread(new Runnable() {
                public void run() { askMicPermission(true); }
            });
        }

        /** Состояние камеры для встроенной съёмки «Снять камерой»: granted / denied / prompt —
         *  страница решает, звать ли getUserMedia или сразу вести пользователя в настройки. */
        @JavascriptInterface
        public String camStatus() {
            if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) return "granted";
            if (prefs.getBoolean("camAsked", false)
                    && !shouldShowRequestPermissionRationale(Manifest.permission.CAMERA)) return "denied";
            return "prompt";
        }

        @JavascriptInterface
        public void askCamera() {
            runOnUiThread(new Runnable() {
                public void run() { askCameraPermission(true); }
            });
        }

        @JavascriptInterface
        public void openSettings() {
            runOnUiThread(new Runnable() {
                public void run() {
                    try {
                        startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                Uri.parse("package:" + getPackageName())));
                    } catch (Exception e) {
                        Toast.makeText(MainActivity.this, "Откройте настройки приложения и включите микрофон или камеру", Toast.LENGTH_LONG).show();
                    }
                }
            });
        }

        /** Диагностика для виджета: кто реально держит микрофон, не идёт ли вызов — факты вместо «занят другим приложением». */
        @JavascriptInterface
        public String micDiag() {
            try {
                return micDiagUi();
            } catch (Exception e) {
                return "диагностика недоступна: " + e.getClass().getSimpleName();
            }
        }

        /**
         * Нативная запись голоса: start / stop (готовый файл в base64) / cancel.
         * WebView на части устройств не может поднять микрофон (getUserMedia → NotReadableError),
         * хотя обычные приложения пишут звук без проблем — тогда страница пишет через этот мост.
         */
        @JavascriptInterface
        public String nativeRec(final String action) {
            try {
                return callOnUi(new Callable<String>() {
                    public String call() { return nativeRecUi(action); }
                });
            } catch (Exception e) {
                return "{\"ok\":false,\"err\":\"" + e.getClass().getSimpleName() + "\"}";
            }
        }
    }

    /** Выполнить на UI-потоке и дождаться результата. JS-мост работает в фоновом потоке, а MediaRecorder — только из UI. */
    private String callOnUi(Callable<String> c) throws Exception {
        if (Looper.myLooper() == Looper.getMainLooper()) return c.call();
        FutureTask<String> ft = new FutureTask<>(c);
        runOnUiThread(ft);
        return ft.get(5, TimeUnit.SECONDS);
    }

    /** Кто держит микрофон, включён ли mute и не идёт ли вызов — факты вместо «занят другим приложением». */
    private String micDiagUi() {
        AudioManager am = (AudioManager) getSystemService(AUDIO_SERVICE);
        if (am == null) return "аудиосервис недоступен";
        StringBuilder sb = new StringBuilder();
        sb.append("активная запись: ");
        List<AudioRecordingConfiguration> cfgs = am.getActiveRecordingConfigurations();
        if (cfgs.isEmpty()) sb.append("не видно (Android скрывает чужие записи, или их нет)");
        else for (AudioRecordingConfiguration c : cfgs) {   // имя держателя микрофона Android не отдаёт (нет публичного API) — показываем факты, которые есть
            sb.append("сессия ").append(c.getClientAudioSessionId())
              .append(" (источник ").append(c.getClientAudioSource())
              .append(", приглушена: ").append(c.isClientSilenced() ? "да" : "нет").append(") ");
        }
        sb.append(" · микрофон заглушён: ").append(am.isMicrophoneMute() ? "да" : "нет");
        int mode = am.getMode();
        String[] modes = {"норма", "рингтон", "телефонный вызов", "VoIP-вызов"};
        sb.append(" · режим звука: ").append(mode >= 0 && mode < modes.length ? modes[mode] : String.valueOf(mode));
        return sb.toString();
    }

    /* ===== Нативная запись голоса (см. JsBridge.nativeRec) =====
       Пишем через системный MediaRecorder — тот же путь, что и у приложений-рекордеров, —
       и отдаём странице готовый файл. getUserMedia остаётся основным путём, это запасной. */
    private String nativeRecUi(String action) {
        if ("cancel".equals(action)) { releaseNativeRec(true); return "{\"ok\":true}"; }
        if ("stop".equals(action)) {
            if (micRec == null) return "{\"ok\":false,\"err\":\"not-recording\"}";
            try {
                micRec.stop();
            } catch (Exception e) {
                releaseNativeRec(true);
                return "{\"ok\":false,\"err\":\"stop-failed\"}";
            }
            String b64;
            long dur = Math.max(1, (System.currentTimeMillis() - micT0) / 1000);
            try {
                b64 = readB64(micFile);
            } catch (Exception e) {
                releaseNativeRec(true);
                return "{\"ok\":false,\"err\":\"read-failed\"}";
            }
            releaseNativeRec(true);
            return "{\"ok\":true,\"audio\":\"" + b64 + "\",\"mime\":\"audio/mp4\",\"dur\":" + dur + "}";
        }
        if (!"start".equals(action)) return "{\"ok\":false,\"err\":\"bad-action\"}";
        if (micRec != null) return "{\"ok\":true}";                        // уже пишем
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            askMicPermission(true);
            return "{\"ok\":false,\"err\":\"noperm\"}";
        }
        File dir = getCacheDir();
        if (dir == null) return "{\"ok\":false,\"err\":\"no-cache\"}";
        micFile = new File(dir, "mic-" + System.currentTimeMillis() + ".m4a");
        micT0 = System.currentTimeMillis();
        // MIC не поднялся (занят или не поддержан) — пробуем запасные источники записи
        int[] sources = {MediaRecorder.AudioSource.MIC, MediaRecorder.AudioSource.VOICE_RECOGNITION, MediaRecorder.AudioSource.DEFAULT};
        String err = "start-failed";
        for (int src : sources) {
            MediaRecorder r = new MediaRecorder();
            try {
                r.setAudioSource(src);
                r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);
                r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC);
                r.setAudioChannels(1);
                r.setAudioSamplingRate(16000);
                r.setAudioEncodingBitRate(32000);
                r.setOutputFile(micFile.getAbsolutePath());
                r.prepare();
                r.start();
                micRec = r;
                return "{\"ok\":true}";
            } catch (Exception e) {
                err = e.getClass().getSimpleName();
                try { r.reset(); } catch (Exception ignored) {}
                try { r.release(); } catch (Exception ignored) {}
            }
        }
        releaseNativeRec(true);
        return "{\"ok\":false,\"err\":\"" + err + "\"}";
    }

    private void releaseNativeRec(boolean deleteFile) {
        if (micRec != null) {
            try { micRec.reset(); } catch (Exception ignored) {}
            try { micRec.release(); } catch (Exception ignored) {}
            micRec = null;
        }
        if (deleteFile && micFile != null) {
            try { micFile.delete(); } catch (Exception ignored) {}
            micFile = null;
        }
    }

    private static String readB64(File f) throws Exception {
        InputStream in = null;
        try {
            in = new FileInputStream(f);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            return Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP);   // NO_WRAP: JS atob не переваривает переводы строк
        } finally {
            if (in != null) try { in.close(); } catch (Exception ignored) {}
        }
    }

    private static String safePdfName(String name) {
        String fn = (name == null) ? "" : name.trim().replace('/', '_').replace('\\', '_');
        return fn.isEmpty() ? "raspisanie.pdf" : fn;
    }

    /** «raspisanie_2026-10-12.pdf» + попытка 1 → «raspisanie_2026-10-12 (1).pdf». */
    private static String numberedName(String fn, int attempt) {
        if (attempt <= 0) return fn;
        int dot = fn.lastIndexOf('.');
        return dot > 0 ? fn.substring(0, dot) + " (" + attempt + ")" + fn.substring(dot) : fn + " (" + attempt + ")";
    }

    private void toastLong(String msg) {
        Toast.makeText(MainActivity.this, msg, Toast.LENGTH_LONG).show();
    }

    /** Запуск загрузки + ожидание её окончания. Только в UI-потоке. attempt — номер повтора при занятом имени файла.
     *  open=false — системное окно ПЕРЕСЫЛКИ файла (кнопка «Поделиться»);
     *  open=true — файл ОТКРЫВАЕТСЯ на экране системным просмотрщиком (кнопка «Скачать» и тап по файлу). */
    private void startShare(final String url, final String name, final String mime, final int attempt) {
        startDownload(url, name, mime, attempt, false);
    }

    private void startDownload(final String url, final String name, final String mime, final int attempt, final boolean open) {
        try {
            final DownloadManager dm = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
            final String fn = numberedName(safePdfName(name), attempt);
            DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
            req.setMimeType(mime);
            req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                // Android 10+: в публичные «Загрузки» можно писать через DownloadManager без разрешений
                req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fn);
            } else {
                // Android 7–9: запись в публичные «Загрузки» требует WRITE_EXTERNAL_STORAGE (runtime-разрешение),
                // а для шаринга оно не нужно — кладём файл в личную папку приложения (разрешений не просит)
                File dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
                if (dir == null) throw new IllegalStateException("no external files dir");
                new File(dir, fn).delete();      // свежий PDF вместо старого: расписание могло измениться
                req.setDestinationInExternalFilesDir(this, Environment.DIRECTORY_DOWNLOADS, fn);
            }
            final long id = dm.enqueue(req);
            BroadcastReceiver rc = new BroadcastReceiver() {
                @Override
                public void onReceive(Context c, Intent i) {
                    long rid = (i == null) ? -1 : i.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1);
                    if (rid != id) return;
                    try { unregisterReceiver(this); } catch (Exception ignored) {}
                    shareReceivers.remove(this);
                    // ACTION_DOWNLOAD_COMPLETE приходит и при неудачной загрузке — смотрим фактический статус
                    int status = -1, reason = 0;
                    Cursor cur = null;
                    try {
                        cur = dm.query(new DownloadManager.Query().setFilterById(id));
                        if (cur != null && cur.moveToFirst()) {
                            status = cur.getInt(cur.getColumnIndex(DownloadManager.COLUMN_STATUS));
                            reason = cur.getInt(cur.getColumnIndex(DownloadManager.COLUMN_REASON));
                        }
                    } catch (Exception ignored) {
                    } finally {
                        if (cur != null) cur.close();
                    }
                    if (status != DownloadManager.STATUS_SUCCESSFUL) {
                        if (status == DownloadManager.STATUS_FAILED
                                && reason == DownloadManager.ERROR_FILE_ALREADY_EXISTS && attempt < 3) {
                            startDownload(url, name, mime, attempt + 1, open);     // такой файл уже лежит в «Загрузках» — другое имя
                            return;
                        }
                        toastLong("Не получилось скачать файл — попробуйте ещё раз");
                        return;
                    }
                    Uri u = dm.getUriForDownloadedFile(id);
                    if (u == null) {
                        toastLong(open ? "Файл скачался, но открыть его не получилось"
                                : "Файл скачался, но открыть окно пересылки не получилось");
                        return;
                    }
                    if (open) {   // «Скачать»/тап по файлу: открыть документ на экране системным просмотрщиком
                        try {
                            Intent v = new Intent(Intent.ACTION_VIEW);
                            v.setDataAndType(u, mime);
                            v.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                            startActivity(Intent.createChooser(v, "Открыть файл"));
                        } catch (Exception e) {
                            toastLong("Файл скачался — нет приложения для открытия этого файла");
                        }
                        return;
                    }
                    try {
                        Intent s = new Intent(Intent.ACTION_SEND);
                        s.setType(mime);
                        s.putExtra(Intent.EXTRA_STREAM, u);
                        s.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                        startActivity(Intent.createChooser(s, "Переслать файл"));
                    } catch (Exception e) {
                        toastLong("Не получилось открыть окно пересылки");
                    }
                }
            };
            IntentFilter f = new IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE);
            // Android 14: broadcast DownloadManager приходит из ЧУЖОГО процесса (downloads provider),
            // поэтому NOT_EXPORTED-регистрация его не получает — окно «Переслать файл» не открывалось.
            // Защита от подделки — сверка EXTRA_DOWNLOAD_ID с нашим id (выше в onReceive).
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) registerReceiver(rc, f, Context.RECEIVER_EXPORTED);
            else registerReceiver(rc, f);
            shareReceivers.add(rc);
        } catch (Exception e) {
            toastLong(open ? "Не получилось скачать файл" : "Не получилось поделиться файлом");
        }
    }

    /**
     * Сворачиваем — освобождаем микрофон и гасим WebView. Иначе запись, забытая в фоне,
     * держит микрофон, и следующая попытка падает «микрофон занят другим приложением».
     */
    @Override
    protected void onPause() {
        releaseNativeRec(true);
        if (web != null) {
            try { web.evaluateJavascript("window.__lpPause&&window.__lpPause()", null); } catch (Exception ignored) {}
            webPaused = true;
            web.postDelayed(new Runnable() {
                public void run() {
                    if (web != null && webPaused) web.onPause();   // даём странице сначала отпустить поток записи
                }
            }, 150);
        }
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        webPaused = false;
        if (web != null) { web.onResume(); pollTelegramLogin(); }
    }

    /** Возврат из Telegram по ссылке SCHEME://login (страница бота /app-return): singleTask доставляет её сюда. */
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        pollTelegramLogin();
    }

    /** «Войти через Telegram»: пока приложение было в фоне, таймеры страницы спали — просим забрать токен сразу. */
    private void pollTelegramLogin() {
        if (web == null) return;
        try { web.evaluateJavascript("window.__tgPoll&&window.__tgPoll()", null); } catch (Exception ignored) {}
    }

    @Override
    protected void onDestroy() {
        releaseNativeRec(false);
        for (BroadcastReceiver r : new ArrayList<>(shareReceivers)) {
            try { unregisterReceiver(r); } catch (Exception ignored) {}
        }
        shareReceivers.clear();
        super.onDestroy();
    }

    /** Ответ пользователя на системный запрос микрофона/камеры. Страница у него pending-запроса не держит — просто подсказываем следующий шаг. */
    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != MIC_PERM_REQ && requestCode != CAMERA_PERM_REQ) return;
        boolean granted = grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED;
        if (requestCode == CAMERA_PERM_REQ) {
            if (granted) {
                Toast.makeText(this, "Камера разрешена — нажмите кнопку ещё раз", Toast.LENGTH_SHORT).show();
            } else {
                Toast.makeText(this, "Нет доступа к камере — включите его в настройках приложения", Toast.LENGTH_LONG).show();
            }
            return;
        }
        if (granted) {
            Toast.makeText(this, "Микрофон разрешён — нажмите кнопку ещё раз", Toast.LENGTH_SHORT).show();
        } else {
            Toast.makeText(this, "Нет доступа к микрофону — включите его в настройках приложения", Toast.LENGTH_LONG).show();
        }
    }

    /** Результат выбора файла/фото → обратно в страницу (<input type="file">).
     *  Системная камера отдаёт снимок через EXTRA_OUTPUT и пустой intent — берём файл из captureFile. */
    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if ((requestCode == FILE_CHOOSER_REQ || requestCode == CAMERA_REQ) && fileCb != null) {
            ValueCallback<Uri[]> cb = fileCb;
            fileCb = null;
            Uri[] res = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
            File cap = captureFile;
            captureFile = null;
            if (res == null && cap != null) {
                if (resultCode == RESULT_OK && cap.length() > 0) res = new Uri[]{CaptureProvider.uriFor(this, cap)};
                else cap.delete();                     // снимок отменили — пустой файл не оставляем
            }
            cb.onReceiveValue(res);
        }
    }

    /**
     * «Снять камерой»: системное приложение камеры пишет снимок в content:// URI
     * (см. CaptureProvider — file:// запрещён с Android 7, а без EXTRA_OUTPUT камера
     * возвращает только маленькую превьюшку), и страница получает файл в свой <input>.
     * Разрешения CAMERA не требуется: снимок делает стороннее приложение камеры.
     * ВАЖНО: resolveActivity() на Android 11+ без <queries> в манифесте всегда возвращает null
     * (package visibility): раньше из-за такой «проверки» камера молча не открывалась, и тихо
     * уходили в галерею. Проверяем делом — startActivityForResult сам бросит исключение.
     * false — камеры нет/упала: откатываемся на обычный выбор файла.
     */
    private boolean launchCamera(WebChromeClient.FileChooserParams params) {
        try {
            String[] types = params == null ? null : params.getAcceptTypes();
            boolean video = types != null && types.length > 0 && types[0] != null && types[0].startsWith("video");
            File dir = new File(getCacheDir(), "captures");
            if (!dir.isDirectory() && !dir.mkdirs()) throw new IllegalStateException("captures");
            captureFile = new File(dir, "cap-" + System.currentTimeMillis() + (video ? ".mp4" : ".jpg"));
            Uri out = CaptureProvider.uriFor(MainActivity.this, captureFile);
            Intent cam = new Intent(video ? MediaStore.ACTION_VIDEO_CAPTURE : MediaStore.ACTION_IMAGE_CAPTURE);
            cam.putExtra(MediaStore.EXTRA_OUTPUT, out);
            cam.setClipData(ClipData.newRawUri("", out));   // grant-флаги доходят до камеры и на старых API
            cam.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
            startActivityForResult(cam, CAMERA_REQ);
            return true;
        } catch (Exception e) {
            // камеры нет или она недоступна — сообщаем ГРОМКО и уходим в выбор картинки
            captureFile = null;
            Toast.makeText(MainActivity.this, "Камера недоступна — открываю выбор картинки", Toast.LENGTH_LONG).show();
            return false;
        }
    }

    /** accept-типы страницы приходят расширениями («.pdf,.docx») — WebView кладёт их в EXTRA_MIME_TYPES
     *  как есть, и системный выборщик не показывает документы. Раньше тип ещё и принудительно
     *  ставился image/* — «Файл» в чате с ИИ вообще не мог прикрепить PDF/Word.
     *  Приводим расширения к MIME-типам и не мешаем множественному выбору. */
    private void fixChooserTypes(Intent i, WebChromeClient.FileChooserParams params) {
        String[] acc = params == null ? null : params.getAcceptTypes();
        if (acc == null || acc.length == 0) {
            if (i.getType() == null) i.setType("*/*");
            return;
        }
        List<String> mimes = new ArrayList<>();
        for (String a : acc) {
            String m = extToMime(a);
            if (m != null && !mimes.contains(m)) mimes.add(m);
        }
        String cur = i.getType();
        if (mimes.isEmpty()) {                      // расширения незнакомые — не фильтруем вообще
            if (cur == null || !cur.contains("/")) i.setType("*/*");
            i.removeExtra(Intent.EXTRA_MIME_TYPES);
            return;
        }
        if (cur == null || !cur.contains("/")) i.setType(mimes.size() == 1 ? mimes.get(0) : "*/*");
        i.putExtra(Intent.EXTRA_MIME_TYPES, mimes.toArray(new String[0]));
    }

    /** Расширение (или MIME) из accept= → MIME-тип для системного выборщика файла. */
    private static String extToMime(String accept) {
        if (accept == null) return null;
        String s = accept.trim().toLowerCase();
        if (s.isEmpty()) return null;
        if (s.contains("/")) return s;
        if (s.startsWith(".")) s = s.substring(1);
        switch (s) {
            case "pdf": return "application/pdf";
            case "doc": return "application/msword";
            case "docx": return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
            case "xls": return "application/vnd.ms-excel";
            case "xlsx": return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
            case "ppt": return "application/vnd.ms-powerpoint";
            case "pptx": return "application/vnd.openxmlformats-officedocument.presentationml.presentation";
            case "txt": return "text/plain";
            case "md": return "text/markdown";
            case "csv": return "text/csv";
            case "rtf": return "application/rtf";
            case "jpg": case "jpeg": return "image/jpeg";
            case "png": return "image/png";
            case "gif": return "image/gif";
            case "webp": return "image/webp";
            case "mp3": return "audio/mpeg";
            case "m4a": return "audio/mp4";
            case "mp4": return "video/mp4";
            case "zip": return "application/zip";
            default: return null;
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
