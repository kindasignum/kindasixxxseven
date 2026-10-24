"""
Работа с Google Таблицей (gspread + сервисный аккаунт).

ЗАПИСЬ
    write_training(training: dict) -> dict      — записать тренировку (идемпотентно)
ЧТЕНИЕ
    read_trainings(date_from=None, date_to=None)      -> list[dict]
    read_participations(date_from=None, date_to=None, vk_id=None, name=None) -> list[dict]
    points_summary(date_from=None, date_to=None)      -> list[dict]  (баллы по участникам)
    has_training(key) -> bool
СОПОСТАВЛЕНИЕ С ЛИСТОМ «УЧАСТНИКИ»
    load_members(force=False) -> Members        — читает лист (кэш 5 минут)
    Members.trainer(vk_id)                      — персонаж тренера по «ID вк»
    Members.participant(name, vk_id=None)       — персонаж участника по «Имя на варе»
СЛУЖЕБНОЕ
    check_connection() -> dict                  — проверка доступа, создание листов

Командная строка:
    python sheets.py check                      — проверить подключение
    python sheets.py members                    — проверить чтение листа «Участники»
    python sheets.py who 123456                 — найти персонажа по VK ID
    python sheets.py find Лихушек               — найти персонажа по имени
    python sheets.py test-write                 — записать тестовую тренировку
    python sheets.py summary [дд.мм.гггг дд.мм.гггг]

Столбцы листов «Тренировки» и «Участия» ищутся ПО НАЗВАНИЯМ в первой строке: порядок можно менять,
свои столбцы — добавлять; недостающие столбцы бот дописывает справа сам.

Формат `training` (его собирает bot.build_training_payload):
    {
      "key": "2000000001-1761300000",           # уникальный ключ: ID беседы + время начала
      "start": datetime, "end": datetime,       # с часовым поясом МСК
      "chat_id": 1, "chat_title": "Название",
      "leader_id": 123, "leader_name": "Имя тренера", "leader_cw_id": 777 | "",
      "participants": [
        {"vk_id": 456 | None, "cw_id": 888 | "", "name": "Иван", "join": datetime,
         "leave": datetime, "minutes": 80, "points": 1.5, "auto_leave": False},
      ],
    }
"""
<<<<<<< HEAD
import base64
=======
>>>>>>> c46da25c8ce711c7fdbe19aa5fc9b80d1953bbfb
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("trainbot.sheets")
<<<<<<< HEAD
=======

>>>>>>> c46da25c8ce711c7fdbe19aa5fc9b80d1953bbfb
MSK = ZoneInfo("Europe/Moscow")

TRAININGS_HEADERS = [
    "Ключ", "Дата начала", "Дата окончания", "Начало", "Окончание", "ID беседы",
    "Беседа", "ID ведущего", "CW ID тренера", "Ведущий", "Участников", "Сумма баллов", "Записано",
]
PARTS_HEADERS = [
    "Ключ", "Дата тренировки", "Беседа", "Ведущий", "VK ID", "CW ID участника", "Имя",
    "Пришёл", "Ушёл", "Минут", "Баллы", "Самостоятельный уход", "Записано",
]
MEMBERS_TTL = 300  # сколько секунд хранить прочитанный лист «Участники»


class SheetsError(Exception):
    """Понятная человеку ошибка работы с таблицей."""


# ───────────────────────── подключение ─────────────────────────
def _cfg():
    creds = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
    if not os.path.isabs(creds) and not os.path.exists(creds):
        alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), creds)
        if os.path.exists(alt):
            creds = alt
    return (
        creds,
        os.getenv("GOOGLE_SHEET_ID", "").strip(),
        os.getenv("SHEET_TRAININGS", "Тренировки"),
        os.getenv("SHEET_PARTICIPATIONS", "Участия"),
    )


<<<<<<< HEAD
def _credentials_info():
    """
    Ключ сервисного аккаунта из переменной окружения GOOGLE_CREDENTIALS_JSON (для хостинга,
    где нельзя хранить файл-ключ в репозитории). Значение — либо сам JSON, либо он же в base64
    (base64 надёжнее: в нём нет кавычек, переносов и обратных слэшей). None — переменная не задана.
    """
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not raw:
        return None
    if raw[0] in "\"'":  # случайно вставили вместе с кавычками
        raw = raw.strip("\"'")
    if not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        except Exception:  # noqa: BLE001
            raise SheetsError("GOOGLE_CREDENTIALS_JSON — это не JSON и не корректный base64. "
                              "Получите значение командой: python encode_credentials.py google_credentials.json")
    try:
        info = json.loads(raw, strict=False)  # strict=False терпит переносы строк внутри private_key
    except json.JSONDecodeError as e:
        raise SheetsError(f"GOOGLE_CREDENTIALS_JSON содержит некорректный JSON: {e}")
    if "client_email" not in info or "private_key" not in info:
        raise SheetsError("В GOOGLE_CREDENTIALS_JSON нет client_email/private_key — нужен JSON-ключ сервисного аккаунта")
    info["private_key"] = info["private_key"].replace("\\n", "\n")
    return info


=======
>>>>>>> c46da25c8ce711c7fdbe19aa5fc9b80d1953bbfb
_sh_cache = {}


def _spreadsheet(sheet_id: str = ""):
    creds, default_id, _, _ = _cfg()
    sheet_id = (sheet_id or default_id).strip()
    if sheet_id in _sh_cache:
        return _sh_cache[sheet_id]
    if not sheet_id:
<<<<<<< HEAD
        raise SheetsError("Не задан GOOGLE_SHEET_ID (.env или переменные окружения хостинга)")
    info = _credentials_info()
    if info is None and not os.path.exists(creds):
        raise SheetsError("Не найден ключ Google: задайте переменную GOOGLE_CREDENTIALS_JSON "
                          f"(хостинг) или положите файл {creds} (локально, GOOGLE_CREDENTIALS_FILE)")
=======
        raise SheetsError("Не задан GOOGLE_SHEET_ID в .env")
    if not os.path.exists(creds):
        raise SheetsError(f"Не найден файл ключа Google: {creds} (GOOGLE_CREDENTIALS_FILE)")
>>>>>>> c46da25c8ce711c7fdbe19aa5fc9b80d1953bbfb
    try:
        import gspread
    except ImportError:
        raise SheetsError("Не установлен gspread: pip install -r requirements.txt")
    try:
<<<<<<< HEAD
        client = gspread.service_account_from_dict(info) if info else gspread.service_account(filename=creds)
        _sh_cache[sheet_id] = client.open_by_key(sheet_id)
=======
        _sh_cache[sheet_id] = gspread.service_account(filename=creds).open_by_key(sheet_id)
>>>>>>> c46da25c8ce711c7fdbe19aa5fc9b80d1953bbfb
    except Exception as e:  # noqa: BLE001
        raise SheetsError(
            f"Не удалось открыть таблицу ({type(e).__name__}: {e}). Проверьте ID таблицы, "
            "доступ сервисного аккаунта (роль «Редактор») и включённые Google Sheets API / Drive API."
        )
    return _sh_cache[sheet_id]


def _retry(fn, *args, **kwargs):
    """Повторяет вызов при лимитах Google (429), 5xx и обрывах сети."""
    delay = 2
    for attempt in range(4):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            code = getattr(getattr(e, "response", None), "status_code", None)
            net = type(e).__name__ in ("ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout")
            if (code in (429, 500, 502, 503, 504) or net) and attempt < 3:
                log.warning("Google API: %s, повтор через %s с", code or type(e).__name__, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise


def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _ensure_ws(name: str, headers):
    """
    Возвращает (лист, список заголовков). Создаёт лист, если его нет; если какого-то из
    нужных столбцов нет — дописывает его заголовок справа (старые данные не трогаются).
    """
    sh = _spreadsheet()
    try:
        ws = _retry(sh.worksheet, name)
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ != "WorksheetNotFound":
            raise
        ws = _retry(sh.add_worksheet, title=name, rows=1000, cols=len(headers))
    head = [str(h).strip() for h in _retry(ws.row_values, 1)]
    if not any(head):
        _retry(ws.update, range_name="A1", values=[list(headers)])
        head = list(headers)
        try:
            _retry(ws.freeze, rows=1)
        except Exception:  # noqa: BLE001  (не критично)
            pass
    else:
        missing = [h for h in headers if h not in head]
        if missing:
            need = len(head) + len(missing)
            have = getattr(ws, "col_count", need)
            if have < need:
                _retry(ws.add_cols, need - have)
            _retry(ws.update, range_name=f"{_col_letter(len(head) + 1)}1", values=[missing])
            head += missing
    return ws, head


def _wrap(fn):
    def inner(*a, **kw):
        try:
            return fn(*a, **kw)
        except SheetsError:
            raise
        except Exception as e:  # noqa: BLE001
            raise SheetsError(f"{type(e).__name__}: {e}")
    inner.__name__ = fn.__name__
    inner.__doc__ = fn.__doc__
    return inner


# ───────────────────────── форматирование ─────────────────────────
def _d(x: datetime) -> str:
    return x.strftime("%d.%m.%Y")


def _dt(x: datetime) -> str:
    return x.strftime("%d.%m.%Y %H:%M")


def _num(x, default=0.0):
    try:
        return float(str(x).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return default


def _parse_date(s: str):
    try:
        return datetime.strptime(str(s).strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def _parse_dt(s: str):
    try:
        return datetime.strptime(str(s).strip(), "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
    except ValueError:
        return None


def _in_range(d, date_from, date_to) -> bool:
    if d is None:
        return False
    return (date_from is None or d >= date_from) and (date_to is None or d <= date_to)


def _row(head, data: dict):
    """Строка в порядке столбцов листа; незнакомые столбцы остаются пустыми."""
    return [data.get(h, "") for h in head]


# ───────────────────────── лист «Участники» ─────────────────────────
def norm_key(s) -> str:
    """Для сравнения имён: без регистра, ё=е, лишние пробелы убраны."""
    return " ".join(str(s).replace("\xa0", " ").casefold().replace("ё", "е").split())


_VK_RE = re.compile(r"^(?:https?://)?(?:m\.)?(?:vk\.(?:com|ru)/)?(?:id)?(\d+)/?$", re.I)


def _parse_vk_id(cell):
    """Число, «id123» или ссылка vk.com/id123 → 123. Короткие адреса (vk.com/ivan) не поддерживаются."""
    s = str(cell).replace("\xa0", "").replace(" ", "").strip()
    s = re.sub(r"\.0+$", "", s)
    m = _VK_RE.match(s)
    return int(m.group(1)) if m else None


def _cw_value(v):
    v = str(v).strip()
    return int(v) if v.isdigit() else v


class Members:
    """Справочник «ВК-страница ↔ персонаж» из листа «Участники»."""

    def __init__(self, recs):
        self.recs = recs
        self.by_vk, self.by_name = {}, {}
        for r in recs:
            if r["vk_id"]:
                self.by_vk.setdefault(r["vk_id"], []).append(r)
            if r["name"]:
                self.by_name.setdefault(norm_key(r["name"]), []).append(r)

    def trainer(self, vk_id):
        """Персонаж тренера по VK ID («ID вк» → «ID вар», «Имя на варе»). None — не найден."""
        recs = self.by_vk.get(vk_id) or []
        recs = [r for r in recs if r["cw_id"] != ""] or recs
        if len(recs) > 1 and len({str(r["cw_id"]) for r in recs}) > 1:
            log.warning("VK ID %s встречается на листе «Участники» несколько раз с разными "
                        "ID вар — взята первая строка", vk_id)
        return recs[0] if recs else None

    def participant(self, name, vk_id=None):
        """Персонаж участника по имени («Имя на варе»). None — не найден или неоднозначно."""
        cands = self.by_name.get(norm_key(name), [])
        if len({str(r["cw_id"]) for r in cands}) <= 1:
            return cands[0] if cands else None
        by_author = [r for r in cands if vk_id and r["vk_id"] == vk_id]
        if len({str(r["cw_id"]) for r in by_author}) == 1:
            return by_author[0]
        log.warning("Имя «%s» неоднозначно на листе «Участники»", name)
        return None


_members_cache = {"ts": 0.0, "data": None, "err": None}


@_wrap
def _load_members_uncached() -> Members:
    col_vk = os.getenv("COL_VK_ID", "ID вк")
    col_cw = os.getenv("COL_CW_ID", "ID вар")
    col_name = os.getenv("COL_CW_NAME", "Имя на варе")
    ws_name = os.getenv("SHEET_MEMBERS", "Участники")
    sh = _spreadsheet(os.getenv("GOOGLE_MEMBERS_SHEET_ID", ""))
    try:
        ws = _retry(sh.worksheet, ws_name)
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "WorksheetNotFound":
            raise SheetsError(f"В таблице нет листа «{ws_name}» (SHEET_MEMBERS)")
        raise
    vals = _retry(ws.get_all_values)
    need = [norm_key(col_vk), norm_key(col_cw), norm_key(col_name)]
    hdr_i = idx = None
    for i, row in enumerate(vals[:15]):  # заголовок не обязан быть в первой строке
        keys = [norm_key(c) for c in row]
        if all(n in keys for n in need):
            hdr_i, idx = i, [keys.index(n) for n in need]
            break
    if hdr_i is None:
        raise SheetsError(f"На листе «{ws_name}» не найдены столбцы «{col_vk}», «{col_cw}», "
                          f"«{col_name}» (названия задаются COL_VK_ID, COL_CW_ID, COL_CW_NAME)")
    recs = []
    for row in vals[hdr_i + 1:]:
        row = row + [""] * (max(idx) + 1 - len(row))
        vk_id, cw_id, name = _parse_vk_id(row[idx[0]]), row[idx[1]].strip(), row[idx[2]].strip()
        if vk_id or name:
            recs.append({"vk_id": vk_id, "cw_id": _cw_value(cw_id), "name": name})
    return Members(recs)


def load_members(force: bool = False) -> Members:
    """Лист «Участники» с кэшем на 5 минут. При сбое обновления отдаёт прежние данные, если они есть."""
    c, now = _members_cache, time.time()
    if not force and c["data"] is not None and now - c["ts"] < MEMBERS_TTL:
        return c["data"]
    if not force and c["data"] is None and c["err"] and now - c["ts"] < 60:
        raise SheetsError(c["err"])
    try:
        data = _load_members_uncached()
    except SheetsError as e:
        if c["data"] is not None:
            log.warning("Не удалось обновить лист «Участники», использую прежние данные: %s", e)
            c["ts"] = now
            return c["data"]
        c.update(ts=now, err=str(e))
        raise
    c.update(ts=now, data=data, err=None)
    return data


# ───────────────────────── запись ─────────────────────────
@_wrap
def has_training(key: str) -> bool:
    _, _, ws_tr, _ = _cfg()
    ws, head = _ensure_ws(ws_tr, TRAININGS_HEADERS)
    return key in set(_retry(ws.col_values, head.index("Ключ") + 1))


@_wrap
def write_training(training: dict) -> dict:
    """
    Записывает тренировку: строки участников на лист «Участия» и итоговую строку
    на лист «Тренировки». Повторный вызов с тем же ключом дубликатов не создаёт.
    """
    _, _, ws_tr_name, ws_p_name = _cfg()
    ws_p, head_p = _ensure_ws(ws_p_name, PARTS_HEADERS)
    ws_t, head_t = _ensure_ws(ws_tr_name, TRAININGS_HEADERS)

    key = training["key"]
    start, end = training["start"], training["end"]
    parts = training["participants"]
    now = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    result = {"participants_written": 0, "training_written": False}

    if parts and key not in set(_retry(ws_p.col_values, head_p.index("Ключ") + 1)):
        rows = [
            _row(head_p, {
                "Ключ": key, "Дата тренировки": _d(start), "Беседа": training["chat_title"],
                "Ведущий": training["leader_name"], "VK ID": p["vk_id"] or "",
                "CW ID участника": p.get("cw_id", ""), "Имя": p["name"],
                "Пришёл": _dt(p["join"]), "Ушёл": _dt(p["leave"]),
                "Минут": int(p["minutes"]), "Баллы": float(p["points"]),
                "Самостоятельный уход": "нет" if p["auto_leave"] else "да", "Записано": now,
            })
            for p in parts
        ]
        _retry(ws_p.append_rows, rows, value_input_option="RAW")
        result["participants_written"] = len(rows)

    if key not in set(_retry(ws_t.col_values, head_t.index("Ключ") + 1)):
        total = round(sum(float(p["points"]) for p in parts), 2)
        _retry(
            ws_t.append_row,
            _row(head_t, {
                "Ключ": key, "Дата начала": _d(start), "Дата окончания": _d(end),
                "Начало": start.strftime("%H:%M"), "Окончание": end.strftime("%H:%M"),
                "ID беседы": training["chat_id"], "Беседа": training["chat_title"],
                "ID ведущего": training["leader_id"],
                "CW ID тренера": training.get("leader_cw_id", ""),
                "Ведущий": training["leader_name"], "Участников": len(parts),
                "Сумма баллов": total, "Записано": now,
            }),
            value_input_option="RAW",
        )
        result["training_written"] = True
    return result


# ───────────────────────── чтение ─────────────────────────
def _records(ws_name: str, headers):
    ws, _ = _ensure_ws(ws_name, headers)
    vals = _retry(ws.get_all_values)
    if len(vals) < 2:
        return []
    head = [str(h).strip() for h in vals[0]]
    return [
        dict(zip(head, r + [""] * (len(head) - len(r))))
        for r in vals[1:] if any(c.strip() for c in r)
    ]


def _int_or_text(v):
    v = str(v).strip()
    return int(v) if v.isdigit() else v


@_wrap
def read_trainings(date_from: date = None, date_to: date = None):
    """Тренировки, начавшиеся в периоде [date_from; date_to] (включительно)."""
    _, _, ws_tr, _ = _cfg()
    out = []
    for r in _records(ws_tr, TRAININGS_HEADERS):
        d = _parse_date(r.get("Дата начала", ""))
        if not _in_range(d, date_from, date_to):
            continue
        out.append({
            "key": r.get("Ключ", ""), "date_start": d,
            "date_end": _parse_date(r.get("Дата окончания", "")),
            "start": r.get("Начало", ""), "end": r.get("Окончание", ""),
            "chat_id": int(_num(r.get("ID беседы", ""))), "chat_title": r.get("Беседа", ""),
            "leader_id": int(_num(r.get("ID ведущего", ""))),
            "leader_cw_id": _int_or_text(r.get("CW ID тренера", "")),
            "leader_name": r.get("Ведущий", ""),
            "participants": int(_num(r.get("Участников", ""))),
            "points": _num(r.get("Сумма баллов", "")), "written": r.get("Записано", ""),
        })
    return out


@_wrap
def read_participations(date_from: date = None, date_to: date = None, vk_id: int = None, name: str = None):
    """Участия по периоду; можно отфильтровать по VK ID или имени."""
    _, _, _, ws_p = _cfg()
    out = []
    for r in _records(ws_p, PARTS_HEADERS):
        d = _parse_date(r.get("Дата тренировки", ""))
        if not _in_range(d, date_from, date_to):
            continue
        vid = int(_num(r.get("VK ID", ""))) if str(r.get("VK ID", "")).strip() else None
        if vk_id is not None and vid != vk_id:
            continue
        if name is not None and norm_key(r.get("Имя", "")) != norm_key(name):
            continue
        out.append({
            "key": r.get("Ключ", ""), "date": d, "chat_title": r.get("Беседа", ""),
            "leader": r.get("Ведущий", ""), "vk_id": vid,
            "cw_id": _int_or_text(r.get("CW ID участника", "")), "name": r.get("Имя", ""),
            "join": _parse_dt(r.get("Пришёл", "")), "leave": _parse_dt(r.get("Ушёл", "")),
            "minutes": int(_num(r.get("Минут", ""))), "points": _num(r.get("Баллы", "")),
            "auto_leave": r.get("Самостоятельный уход", "").strip().lower() == "нет",
            "written": r.get("Записано", ""),
        })
    return out


@_wrap
def points_summary(date_from: date = None, date_to: date = None):
    """Суммарные баллы по участникам за период (по убыванию). Ключ — CW ID, иначе VK ID, иначе имя."""
    acc = {}
    for p in read_participations(date_from, date_to):
        if p["cw_id"] != "":
            k = ("cw", str(p["cw_id"]))
        elif p["vk_id"]:
            k = ("vk", p["vk_id"])
        else:
            k = ("name", norm_key(p["name"]))
        a = acc.setdefault(k, {"cw_id": p["cw_id"], "vk_id": p["vk_id"], "name": p["name"],
                               "trainings": set(), "minutes": 0, "points": 0.0})
        a["name"] = p["name"]
        a["trainings"].add(p["key"])
        a["minutes"] += p["minutes"]
        a["points"] += p["points"]
    res = [
        {"cw_id": a["cw_id"], "vk_id": a["vk_id"], "name": a["name"],
         "trainings": len(a["trainings"]), "minutes": a["minutes"], "points": round(a["points"], 2)}
        for a in acc.values()
    ]
    return sorted(res, key=lambda x: (-x["points"], x["name"]))


# ───────────────────────── проверка ─────────────────────────
@_wrap
def check_connection() -> dict:
    creds, sheet_id, ws_tr, ws_p = _cfg()
    sh = _spreadsheet()
    _ensure_ws(ws_tr, TRAININGS_HEADERS)
    _ensure_ws(ws_p, PARTS_HEADERS)
    try:
<<<<<<< HEAD
        info = _credentials_info()
        email = info["client_email"] if info else json.load(open(creds, encoding="utf-8")).get("client_email")
=======
        email = json.load(open(creds, encoding="utf-8")).get("client_email")
>>>>>>> c46da25c8ce711c7fdbe19aa5fc9b80d1953bbfb
    except Exception:  # noqa: BLE001
        email = None
    try:
        members = {"ok": True, "count": len(load_members(force=True).recs)}
    except SheetsError as e:
        members = {"ok": False, "error": str(e)}
    return {"title": sh.title, "service_account": email,
            "worksheets": [w.title for w in sh.worksheets()], "members": members}


def _test_training():
    now = datetime.now(MSK).replace(second=0, microsecond=0)
    return {
        "key": f"TEST-{int(now.timestamp())}", "start": now, "end": now,
        "chat_id": 0, "chat_title": "ТЕСТ (можно удалить)", "leader_id": 0,
        "leader_name": "Тест Тестов", "leader_cw_id": 1,
        "participants": [{"vk_id": None, "cw_id": 2, "name": "Тест Участник", "join": now,
                          "leave": now, "minutes": 45, "points": 1.0, "auto_leave": False}],
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    try:
        if cmd == "check":
            info = check_connection()
            print("Подключение успешно.")
            print("Таблица:", info["title"])
            print("Сервисный аккаунт:", info["service_account"])
            print("Листы:", ", ".join(info["worksheets"]))
            m = info["members"]
            print("Лист «Участники»:", f"прочитано записей: {m['count']}" if m["ok"] else f"ОШИБКА — {m['error']}")
        elif cmd == "members":
            mem = load_members(force=True)
            withvk = sum(1 for r in mem.recs if r["vk_id"])
            print(f"Записей: {len(mem.recs)}, из них с распознанным VK ID: {withvk}")
            for r in mem.recs[:5]:
                print("  ", r)
        elif cmd == "who":
            r = load_members(force=True).trainer(int(sys.argv[2]))
            print(r or "Не найден")
        elif cmd == "find":
            r = load_members(force=True).participant(" ".join(sys.argv[2:]))
            print(r or "Не найден")
        elif cmd == "test-write":
            print(write_training(_test_training()))
            print("Готово: проверьте листы «Тренировки» и «Участия». Тестовые строки можно удалить.")
        elif cmd == "summary":
            f = _parse_date(sys.argv[2]) if len(sys.argv) > 2 else None
            t = _parse_date(sys.argv[3]) if len(sys.argv) > 3 else None
            for row in points_summary(f, t):
                print(f"{row['name']} (CW {row['cw_id'] or '—'}): {row['points']} б., "
                      f"{row['trainings']} трен., {row['minutes']} мин.")
        else:
            print(__doc__)
    except SheetsError as e:
        sys.exit(f"Ошибка: {e}")
