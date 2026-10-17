package ru.kabinet.tutor;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;

import java.io.File;
import java.io.FileNotFoundException;
import java.util.List;

/** Отдаёт системной камере файл для снимка: content://<пакет>.capture/capture/<имя> → cache/captures/<имя>.
 *
 *  WebView не умеет брать снимок с камеры сам: в onShowFileChooser мы открываем ACTION_IMAGE_CAPTURE
 *  с EXTRA_OUTPUT, и приложение камеры пишет фото в наш файл (см. MainActivity). FileProvider из androidx
 *  в сборке нет (APK собирается без Gradle, одним javac) — поэтому минимальная замена только на эту задачу:
 *  запись файла камерой и чтение его обратно в WebView. Наружу провайдер не отдаётся, доступ — только по
 *  grant-флагам того самого intent (которые мы выставляем при открытии камеры). */
public class CaptureProvider extends ContentProvider {
    private static final String SEG = "capture";

    /** Ссылка на файл снимка для EXTRA_OUTPUT системной камеры. */
    public static Uri uriFor(Context c, File f) {
        return new Uri.Builder().scheme("content").authority(c.getPackageName() + ".capture")
                .appendPath(SEG).appendPath(f.getName()).build();
    }

    /** content://…/capture/<имя> → файл в cache/captures; всё остальное — мимо (в том числе «..»). */
    private File fileFor(Uri uri) {
        Context c = getContext();
        List<String> seg = uri == null ? null : uri.getPathSegments();
        if (c == null || seg == null || seg.size() != 2 || !SEG.equals(seg.get(0))) return null;
        String name = seg.get(1);
        if (name.isEmpty() || name.contains("/") || name.contains("\\") || name.contains("..")) return null;
        return new File(new File(c.getCacheDir(), "captures"), name);
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File f = fileFor(uri);
        if (f == null) throw new FileNotFoundException("capture");
        int m = mode != null && mode.indexOf('w') >= 0
                ? ParcelFileDescriptor.MODE_READ_WRITE | ParcelFileDescriptor.MODE_CREATE
                : ParcelFileDescriptor.MODE_READ_ONLY;
        return ParcelFileDescriptor.open(f, m);
    }

    @Override
    public String getType(Uri uri) {
        return "image/jpeg";
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
        return null;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }
}
