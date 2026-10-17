package ru.kabinet.tutor;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.media.AudioManager;
import android.media.AudioRecordingConfiguration;
import android.media.MediaRecorder;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Looper;
import android.os.ParcelFileDescriptor;
import android.provider.MediaStore;
import android.provider.OpenableColumns;
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
import java.io.FileNotFoundException;
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
    private ValueCallback<Uri[]> fileCb;            // выбор файла / снимка (см. onShowFileChooser)
    private File camFile;                           // куда камера пишет снимок (см. launchCamera)
    private Uri camUri;
    private float lpX, lpY;                         // точка последнего касания — для elementFromPoint при долгом тапе
    private static final int FILE_CHOOSER_REQ = 1001;
    private static final int MIC_PERM_REQ = 1002;
    private static final int CAMERA_REQ = 1003;
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
        // alert / confirm / prompt из страницы + выбор файла (галерея) и съёмка камерой:
        // без onShowFileChooser WebView молча игнорирует <input type="file">
        // («Фото ученика → Выбрать из галереи» ничего не открывало),
        // а без ветки на capture камера открывалась «галереей» вместо съёмки («Снять камерой» в чате ИИ).
        web.setWebChromeClient(new WebChromeClient() {
            /** Голосовой помощник: страница просит микрофон (getUserMedia). Без этого обработчика WebView
             *  молча отклоняет запрос. Разрешаем только звук и только нашему серверу, после системного запроса. */
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(new Runnable() {
                    public void run() {
                        try {
                            String reqHost = request.getOrigin() == null ? null : request.getOrigin().getHost();
                            String nowUrl = web == null ? null : web.getUrl();
                            String nowHost = nowUrl == null ? null : Uri.parse(nowUrl).getHost();
                            boolean ours = reqHost != null && (reqHost.equals(Uri.parse(url).getHost()) || reqHost.equals(nowHost));
                            boolean audio = false;
                            for (String r : request.getResources()) if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)) audio = true;
                            if (!ours || !audio) { request.deny(); return; }
                            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                                request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                                return;
                            }
                            // ВАЖНО: системный диалог поверх pending-запроса WebView отменяет его на части
                            // устройств (onPermissionRequestCanceled), и getUserMedia падает без объяснений —
                            // «Микрофон недоступен». Поэтому запрос страницы закрываем СРАЗУ, разрешение
                            // просим отдельно, а страница повторяет нажатие (см. виджет: micStatus/askMic).
                            request.deny();
                            askMicPermission(true);
                            Toast.makeText(MainActivity.this, "Разрешите микрофон — и нажмите кнопку ещё раз", Toast.LENGTH_LONG).show();
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
                // capture="environment" у <input> — страница просит снять фото/видео КАМЕРОЙ,
                // а не выбирать из галереи («Снять камерой» → «Прикрепите материал к задаче» в чате ИИ)
                if (params != null && params.isCaptureEnabled() && launchCamera(params)) return true;
                Intent i;
                try {
                    i = params != null ? params.createIntent() : new Intent(Intent.ACTION_GET_CONTENT);
                } catch (Exception e) {
                    i = new Intent(Intent.ACTION_GET_CONTENT);
                }
                i.addCategory(Intent.CATEGORY_OPENABLE);
                if (i.getType() == null || i.getType().isEmpty()) i.setType("*/*");   // не навязываем image/*: «Файл» в чате — PDF, Word, TXT
                if (params != null && params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) {
                    i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);   // несколько файлов за раз (вложения чата)
                }
                try {
                    startActivityForResult(i, FILE_CHOOSER_REQ);
                } catch (Exception e) {
                    fileCb = null;
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
        askMicPermission(false);
    }

    /**
     * Запрос микрофона. Просим заранее: пока системный диалог открыт, страница
     * НЕ должна держать pending-запрос getUserMedia — на части устройств такой
     * конфликт отменяет запрос WebView, и запись падает без объяснений.
     * force=false — предзапрос при открытии («больше не спрашивать» уважаем);
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
     * «Снять камерой»: системное приложение камеры пишет снимок в content:// URI
     * (см. CamFileProvider — file:// запрещён с Android 7, а без EXTRA_OUTPUT камера
     * возвращает только маленькую превьюшку) и страница получает файл в свой <input>.
     * Разрешения CAMERA не требуется: снимок делает стороннее приложение камеры.
     * false — камеры нет/упала: откатываемся на обычный выбор файла.
     */
    private boolean launchCamera(WebChromeClient.FileChooserParams params) {
        try {
            String[] types = params == null ? null : params.getAcceptTypes();
            boolean video = types != null && types.length > 0 && types[0] != null && types[0].startsWith("video");
            Intent i = new Intent(video ? MediaStore.ACTION_VIDEO_CAPTURE : MediaStore.ACTION_IMAGE_CAPTURE);
            File dir = getCacheDir();
            if (dir == null) return false;
            camFile = new File(dir, "cam-" + System.currentTimeMillis() + (video ? ".mp4" : ".jpg"));
            camUri = CamFileProvider.uriFor(this, camFile);
            i.putExtra(MediaStore.EXTRA_OUTPUT, camUri);
            i.setClipData(ClipData.newRawUri("", camUri));   // доступ камеры к файлу на всех версиях Android
            i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
            startActivityForResult(i, CAMERA_REQ);
            return true;
        } catch (Exception e) {
            camFile = null;
            camUri = null;
            return false;
        }
    }

    /**
     * JS-мост для страницы (вызывается как Android.*):
     *  - sharePdf(url, name) — «Поделиться PDF» недели: скачивание + системный chooser;
     *  - shareFile(url, name, mime) — «Скачать/Переслать» файла из чата ИИ в любом формате;
     *  - micStatus() — состояние микрофона: granted / denied / prompt — виджет решает,
     *    звать ли getUserMedia или сразу вести пользователя в настройки;
     *  - askMic() — явный системный запрос микрофона (без pending-запроса WebView);
     *  - openSettings() — настройки приложения: включить микрофон после «больше не спрашивать».
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
         *  (Telegram и другие контакты) — для файлов в любом формате. */
        @JavascriptInterface
        public void shareFile(final String url, final String name, final String mime) {
            runOnUiThread(new Runnable() {
                public void run() {
                    startShare(url, name, (mime == null || mime.isEmpty()) ? "application/octet-stream" : mime, 0);
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

        @JavascriptInterface
        public void openSettings() {
            runOnUiThread(new Runnable() {
                public void run() {
                    try {
                        startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                Uri.parse("package:" + getPackageName())));
                    } catch (Exception e) {
                        Toast.makeText(MainActivity.this, "Откройте настройки приложения и включите микрофон", Toast.LENGTH_LONG).show();
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

    /** Запуск загрузки + ожидание её окончания. Только в UI-потоке. attempt — номер повтора при занятом имени файла. */
    private void startShare(final String url, final String name, final String mime, final int attempt) {
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
                            startShare(url, name, mime, attempt + 1);     // такой файл уже лежит в «Загрузках» — другое имя
                            return;
                        }
                        toastLong("Не получилось скачать файл — попробуйте ещё раз");
                        return;
                    }
                    Uri u = dm.getUriForDownloadedFile(id);
                    if (u == null) {
                        toastLong("Файл скачался, но открыть окно пересылки не получилось");
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
            toastLong("Не получилось поделиться файлом");
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
        if (web != null) web.onResume();
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

    /** Ответ пользователя на системный запрос микрофона. Страница у него pending-запроса не держит — просто подсказываем следующий шаг. */
    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != MIC_PERM_REQ) return;
        boolean granted = grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED;
        if (granted) {
            Toast.makeText(this, "Микрофон разрешён — нажмите кнопку ещё раз", Toast.LENGTH_SHORT).show();
        } else {
            Toast.makeText(this, "Нет доступа к микрофону — включите его в настройках приложения", Toast.LENGTH_LONG).show();
        }
    }

    /** Результат выбора файла из галереи (в том числе нескольких) или съёмки камерой → обратно в страницу (<input type="file">). */
    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (fileCb == null) return;
        if (requestCode == CAMERA_REQ) {
            Uri[] out = null;
            if (resultCode == RESULT_OK) {
                if (camUri != null && camFile != null && camFile.length() > 0) out = new Uri[]{camUri};
                else if (data != null && data.getData() != null) out = new Uri[]{data.getData()};
            }
            fileCb.onReceiveValue(out);
            fileCb = null;
            camFile = null;
            camUri = null;
            return;
        }
        if (requestCode == FILE_CHOOSER_REQ) {
            Uri[] out = null;
            if (resultCode == RESULT_OK && data != null) {
                ArrayList<Uri> uris = new ArrayList<>();
                if (data.getClipData() != null) {          // выбрано несколько файлов (EXTRA_ALLOW_MULTIPLE)
                    for (int k = 0; k < data.getClipData().getItemCount(); k++) {
                        Uri u = data.getClipData().getItemAt(k).getUri();
                        if (u != null) uris.add(u);
                    }
                } else if (data.getData() != null) {
                    uris.add(data.getData());
                }
                if (!uris.isEmpty()) out = uris.toArray(new Uri[0]);
            }
            fileCb.onReceiveValue(out);
            fileCb = null;
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

/**
 * Минимальный провайдер файлов для камеры: отдаёт файл из cacheDir по content://-ссылке
 * (см. launchCamera). Нужен, чтобы «Снять камерой» в чате ИИ работало на всех Android:
 * file://-ссылки запрещены с Android 7, а без EXTRA_OUTPUT камера возвращает лишь превьюшку.
 * Провайдер ничего не отдаёт наружу: только файлы из своего cacheDir по одному имени
 * (без путей), запись/чтение — по временной гранте Intent'а камеры.
 */
class CamFileProvider extends android.content.ContentProvider {
    static Uri uriFor(Context c, File f) {
        return new Uri.Builder().scheme("content").authority(c.getPackageName() + ".camfiles")
                .appendPath(f.getName()).build();
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    private File fileFor(Uri uri) {
        String name = uri == null ? null : uri.getLastPathSegment();
        Context c = getContext();
        if (c == null || name == null || name.isEmpty()
                || name.contains("/") || name.contains("\\") || name.contains("..")) return null;
        return new File(c.getCacheDir(), name);
    }

    @Override
    public String getType(Uri uri) {
        String n = uri == null ? null : uri.getLastPathSegment();
        if (n == null) return "application/octet-stream";
        if (n.endsWith(".jpg") || n.endsWith(".jpeg")) return "image/jpeg";
        if (n.endsWith(".png")) return "image/png";
        if (n.endsWith(".mp4")) return "video/mp4";
        if (n.endsWith(".3gp")) return "video/3gpp";
        return "application/octet-stream";
    }

    /** Имя и размер файла: некоторые приложения камеры спрашивают их перед записью. */
    @Override
    public Cursor query(Uri uri, String[] proj, String sel, String[] args, String sort) {
        File f = fileFor(uri);
        MatrixCursor c = new MatrixCursor(new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE});
        c.addRow(new Object[]{f == null ? "" : f.getName(), (f != null && f.exists()) ? f.length() : 0});
        return c;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File f = fileFor(uri);
        if (f == null) throw new FileNotFoundException("нет такого файла");
        int m = (mode != null && mode.indexOf('w') >= 0)
                ? ParcelFileDescriptor.MODE_READ_WRITE : ParcelFileDescriptor.MODE_READ_ONLY;
        return ParcelFileDescriptor.open(f, m);
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String sel, String[] args) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String sel, String[] args) {
        return 0;
    }
}
