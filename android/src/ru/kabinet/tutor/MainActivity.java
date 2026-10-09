package ru.kabinet.tutor;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.view.KeyEvent;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Toast;

/** Обёртка кабинета репетитора: WebView поверх того же адреса, что и мини-апп. */
public class MainActivity extends Activity {
    private WebView web;
    private SharedPreferences prefs;

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
        edit.setText(current.isEmpty() ? "https://tutor-bot-cn4a.onrender.com/" : current);
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
        web.setWebChromeClient(new WebChromeClient());   // alert / confirm / prompt из страницы

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
