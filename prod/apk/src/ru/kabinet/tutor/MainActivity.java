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
    private static final int FILE_CHOOSER_REQ = 1001;
    private static final int MIC_PERM_REQ = 1002;
    private PermissionRequest micRequest;          // запрос микрофона из страницы, ждущий ответа пользователя
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
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);

        web.addJavascriptInterface(new ShareBridge(), "Android");   // «Поделиться PDF»: скачивание + системный chooser

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
                String reqHost = request.getOrigin() == null ? null : request.getOrigin().getHost();
                boolean ours = reqHost != null && reqHost.equals(Uri.parse(url).getHost());
                boolean audio = false;
                for (String r : request.getResources()) if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)) audio = true;
                if (!ours || !audio) { request.deny(); return; }
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                    request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                    return;
                }
                if (micRequest != null) micRequest.deny();       // прошлый запрос не завершился
                micRequest = request;
                requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MIC_PERM_REQ);
            }

            @Override
            public void onPermissionRequestCanceled(PermissionRequest request) {
                if (request == micRequest) micRequest = null;
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

        // долгое нажатие — служебное меню (сменить адрес)
        web.setOnLongClickListener(v -> {
            new AlertDialog.Builder(MainActivity.this)
                    .setTitle("Кабинет репетитора")
                    .setItems(new String[]{"Сменить адрес сервера"}, (dlg, which) -> {
                        prefs.edit().remove("url").apply();
                        showSetup(url);
                    })
                    .setNegativeButton("Отмена", null)
                    .show();
            return true;
        });

        web.loadUrl(url);
        ensureMicPermission();
    }

    /**
     * Микрофон лучше выдать заранее: если разрешения ещё нет, WebView зовёт
     * onPermissionRequest и мы уводим фокус в системный диалог — часть устройств
     * в это время отменяет запрос страницы (onPermissionRequestCanceled),
     * и grant потом некому отдать: getUserMedia висит и падает без объяснений.
     * С уже выданным разрешением onPermissionRequest отвечает синхронно.
     * Спрашиваем один раз; «больше не спрашивать» уважаем.
     */
    private void ensureMicPermission() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) return;
        if (prefs.getBoolean("micAsked", false)
                && !shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO)) return;
        prefs.edit().putBoolean("micAsked", true).apply();
        requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MIC_PERM_REQ);
    }

    /**
     * JS-мост «Поделиться PDF» (страница вызывает Android.sharePdf(url, name)).
     * Файл скачивается через DownloadManager И по завершении загрузки открывается системное
     * окно «Переслать файл». Методы моста вызываются из фонового потока — работу переносим в UI-поток.
     */
    private class ShareBridge {
        @JavascriptInterface
        public void sharePdf(final String url, final String name) {
            runOnUiThread(new Runnable() {
                public void run() { startShare(url, name, 0); }
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
        if (micRequest != null) { micRequest.deny(); micRequest = null; }
        super.onDestroy();
    }

    /** Ответ пользователя на системный запрос микрофона → обратно в страницу. */
    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != MIC_PERM_REQ) return;
        boolean granted = grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED;
        PermissionRequest r = micRequest;
        micRequest = null;
        if (r == null) return;                     // предзапрос при открытии кабинета — страница ещё не просила
        if (granted) {
            r.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
        } else {
            r.deny();
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
