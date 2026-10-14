package ru.kabinet.tutor;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
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

import java.io.File;
import java.util.ArrayList;
import java.util.List;

/** Обёртка кабинета репетитора: WebView поверх того же адреса, что и мини-апп. */
public class MainActivity extends Activity {
    private WebView web;
    private SharedPreferences prefs;
    private ValueCallback<Uri[]> fileCb;            // выбор фото из галереи (см. onShowFileChooser)
    private float lpX, lpY;                         // точка последнего касания — для elementFromPoint при долгом тапе
    private static final int FILE_CHOOSER_REQ = 1001;
    private static final int MIC_PERM_REQ = 1002;
    private final List<BroadcastReceiver> shareReceivers = new ArrayList<>();   // ждут окончания загрузки PDF; снимаются в onDestroy

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
        edit.setText(current.isEmpty() ? "https://tutor-bot-y40k.onrender.com/" : current);
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
                String host = u.getHost(), baseHost = Uri.parse(url).getHost();
                if ("https".equals(u.getScheme()) && host != null
                        && (host.equals(baseHost) || host.endsWith(".telegram.org") || host.equals("t.me"))) {
                    return false;                       // свой сервер и Telegram — внутри приложения
                }
                startActivity(new Intent(Intent.ACTION_VIEW, u));   // всё наружу — системным браузером
                return true;
            }
        });
        // alert / confirm / prompt из страницы + выбор файла из галереи:
        // без onShowFileChooser WebView молча игнорирует <input type="file">
        // («Фото ученика → Выбрать из галереи» ничего не открывало).
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
                Intent i;
                try {
                    i = params != null ? params.createIntent() : new Intent(Intent.ACTION_GET_CONTENT);
                } catch (Exception e) {
                    i = new Intent(Intent.ACTION_GET_CONTENT);
                }
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("image/*");
                try {
                    startActivityForResult(i, FILE_CHOOSER_REQ);
                } catch (Exception e) {
                    fileCb = null;
                    Toast.makeText(MainActivity.this, "Не получилось открыть галерею", Toast.LENGTH_LONG).show();
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
     * JS-мост для страницы (вызывается как Android.*):
     *  - sharePdf(url, name) — «Поделиться PDF»: скачивание + системный chooser;
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
                public void run() { startShare(url, name, 0); }
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
    private void startShare(final String url, final String name, final int attempt) {
        try {
            final DownloadManager dm = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
            final String fn = numberedName(safePdfName(name), attempt);
            DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
            req.setMimeType("application/pdf");
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
                            startShare(url, name, attempt + 1);     // такой файл уже лежит в «Загрузках» — другое имя
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
                        s.setType("application/pdf");
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

    @Override
    protected void onDestroy() {
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

    /** Результат выбора фото из галереи → обратно в страницу (<input type="file">). */
    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_CHOOSER_REQ && fileCb != null) {
            fileCb.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
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
