"""«ИИ-проверка»: ПОСЛЕДНЯЯ проверка пользователя переживает выход из Telegram и рестарт сервиса.

Раньше готовый разбор жил только в памяти телефона (экран результата) и в сеансе EC_CHECKS
с TTL 30 минут; постоянно сохранялось лишь то, что пользователь явно «Сохранил за учеником».
Теперь разбор (и фото работы) пишется в таблицу essay_last СРАЗУ, как только он готов, —
даже если клиент уже закрыл приложение. На входе в раздел кнопка «Последняя проверка»
открывает его без повторного запуска модели.

Одна запись на пользователя (tg_id): новая проверка заменяет прежнюю. История «Сохранить
за учеником» (essay_checks) не меняется. Если essay_last ещё пуста (проверки до этой версии),
берётся самая свежая запись истории — без фото, они в истории не хранятся.

Модуль не зависит от aiohttp: q/run передаются снаружи (bot.py), поэтому он тестируется на голой sqlite.
"""
import json

KINDS = ("ege", "oge")
MAX_PHOTOS = 12                    # как EC_MAX в клиенте: не больше 12 фото на проверку
MAX_PHOTO_CHARS = 24_000_000       # base64-символов фото на запись: страховка от раздувания БД


def put(run, uid, kind, result, photos=None):
    """Запоминает готовый разбор как «последнюю проверку» uid. True — записано."""
    if kind not in KINDS or not isinstance(result, dict):
        return False
    ph = [p for p in (photos or []) if isinstance(p, str) and p][:MAX_PHOTOS]
    blob = json.dumps(ph) if ph and sum(len(p) for p in ph) <= MAX_PHOTO_CHARS else None
    run("insert or replace into essay_last(tg_id,kind,result,photos) values(?,?,?,?)",
        uid, kind, json.dumps(result, ensure_ascii=False), blob)
    return True


def _latest(q, uid):
    """(kind, created, result-dict, photos-list) самой свежей проверки uid или None."""
    rows = q("select kind, created, result, photos from essay_last where tg_id=?", uid)
    if rows:
        src, photos = rows[0], rows[0]["photos"]
    else:                          # проверки до этой версии: самая свежая запись истории (фото там нет)
        rows = q("select kind, created, result from essay_checks where tg_id=? order by id desc limit 1", uid)
        if not rows:
            return None
        src, photos = rows[0], None
    try:
        result = json.loads(src["result"])
        ph = json.loads(photos) if photos else []
    except (TypeError, ValueError):
        return None
    if not isinstance(result, dict) or src["kind"] not in KINDS:
        return None
    return src["kind"], src["created"], result, [p for p in ph if isinstance(p, str)] if isinstance(ph, list) else []


def head(q, uid):
    """Шапка для кнопки «Последняя проверка»: тип, дата, итоговый балл. None — проверок ещё не было."""
    t = _latest(q, uid)
    if not t:
        return None
    kind, created, result, _ = t
    sc = result.get("score") if isinstance(result.get("score"), dict) else {}
    return {"kind": kind, "created": created, "got": sc.get("got", 0), "max": sc.get("max", 0),
            "percent": sc.get("percent", 0), "level": sc.get("level", "")}


def full(q, uid):
    """Полный разбор и фото работы — для превью (формат ответа как у /api/essay_check/load + photos)."""
    t = _latest(q, uid)
    if not t:
        return None
    kind, created, result, photos = t
    return {"id": 0, "kind": kind, "created": created, "student_id": None, "student_name": None,
            "result": result, "photos": photos}
