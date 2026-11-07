"""
VK-бот учёта тренировок.

Беседы: молча читает команды (/старт, #лапа, #уход, /стоп) и ставит реакции.
ЛС: после /стоп присылает ведущему отчёт с кнопкой «Подтвердить», принимает правки.
Время в командах: «/старт 10:00», «/стоп 12:00» (иначе — время сообщения).
Настройка бесед: команда /настройка в ЛС сообщества (только администраторы сообщества).
Запись/чтение Google Таблицы — модуль sheets.py.

Запуск:            python bot.py
Список реакций VK: python bot.py --assets
"""
import atexit
import base64
import hashlib
import json
import logging
import os
import random
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
import zlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import vk_api

import freerun
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.exceptions import ApiError
from vk_api.keyboard import VkKeyboard, VkKeyboardColor

try:  # .env необязателен
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ───────────────────────── настройки ─────────────────────────
TOKEN = os.getenv("VK_TOKEN", "")
GROUP_ID = int(os.getenv("VK_GROUP_ID", "0"))
# База хранится в DATA_DIR (на хостинге — постоянный диск, например /app/data) или в DB_PATH
DATA_DIR = os.getenv("DATA_DIR", "").strip()
if not DATA_DIR and not os.getenv("DB_PATH") and os.path.isdir("/app/data") and os.access("/app/data", os.W_OK):
    DATA_DIR = "/app/data"  # постоянный диск хостинга найден сам, переменная DATA_DIR не обязательна
DB_PATH = os.getenv("DB_PATH") or (os.path.join(DATA_DIR, "trainings.db") if DATA_DIR else "trainings.db")
if DB_PATH != ":memory:" and os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
_db_migrated = None
if DB_PATH != ":memory:" and not os.path.exists(DB_PATH):
    # база от старой версии лежала рядом с кодом — один раз переносим её на постоянный диск
    for _cand in ("trainings.db", os.path.join(os.path.dirname(os.path.abspath(__file__)), "trainings.db")):
        if os.path.exists(_cand) and os.path.abspath(_cand) != os.path.abspath(DB_PATH):
            shutil.copy2(_cand, DB_PATH)
            _db_migrated = os.path.abspath(_cand)
            break
MSK = ZoneInfo("Europe/Moscow")
EDIT_LIMIT_SEC = 24 * 3600  # VK разрешает редактировать сообщения сутки
JOIN_TOLERANCE_SEC = 6 * 3600  # насколько раньше старта допускается время прихода при правках
# как часто (сек) перепроверять непонятные «#…»-сообщения на правки; 0 — отключить опрос
EDIT_POLL_SEC = int(os.getenv("EDIT_POLL_SEC", "20"))
# 1 — писать в лог тип каждого полученного события (для диагностики Long Poll)
LOG_EVENTS = os.getenv("LOG_EVENTS", "0") == "1"
# резервная копия базы в Google Таблице (лист «Резерв бота»): 1 — включена; не чаще раза в N секунд
BACKUP_TO_SHEET = os.getenv("BACKUP_TO_SHEET", "1") == "1"
BACKUP_INTERVAL_SEC = int(os.getenv("BACKUP_INTERVAL_SEC", "30"))
# как часто (сек) проверять, не удалили ли сообщения с командами; 0 — отключить. И сколько часов следить за сообщением
DELETE_POLL_SEC = int(os.getenv("DELETE_POLL_SEC", "60"))
DELETE_WATCH_SEC = int(os.getenv("DELETE_WATCH_HOURS", "48")) * 3600
# свободный бег: 1 — только разбирать отписи и писать в лог (ничего не считать и не отправлять)
FREE_RUN_SHADOW = os.getenv("FREE_RUN_SHADOW", "0") == "1"
# через сколько секунд после последней отписи (когда все ушли) забег закрывается и уходит отчёт админам
FREE_RUN_GRACE_SEC = int(os.getenv("FREE_RUN_GRACE_SEC", str(20 * 60)))
# если за это время не отписали уход — участник закрывается с пометкой «нет ухода» (8 часов)
FREE_RUN_MAX_SEC = int(os.getenv("FREE_RUN_MAX_SEC", str(8 * 3600)))
# новые беседы по умолчанию включены (0 — выключены до команды «N вкл»)
DEFAULT_CHAT_ENABLED = int(os.getenv("DEFAULT_CHAT_ENABLED", "1"))
# запасной список админов сообщества (если VK не отдаёт список): VK ID через запятую
ENV_ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("BOT_ADMIN_IDS", ""))}
# запасной ID владельца сообщества для отладочных сообщений (если VK не отдаёт список руководителей)
ENV_OWNER_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("BOT_OWNER_IDS", ""))}


def _rid(name):
    v = os.getenv(name)
    return int(v) if v else None


# ID реакций берутся из `python bot.py --assets` (см. README)
REACTIONS = {
    "up": _rid("REACTION_THUMBS_UP"),      # 👍 — /старт принят
    "eye": _rid("REACTION_EYE"),           # 👁 — #лапа принят
    "check": _rid("REACTION_CHECK"),       # ✔ — #уход принят / правка принята
    "question": _rid("REACTION_QUESTION"), # ❓ — правка не распознана
}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("trainbot")
if _db_migrated:
    log.info("Найдена база от прежней версии (%s) — скопирована в %s", _db_migrated, os.path.abspath(DB_PATH))

vk_session = None
vk = None

# ───────────────────────── база данных ─────────────────────────
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS trainings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id INTEGER NOT NULL,
    leader_id INTEGER NOT NULL,      -- кто написал /старт
    stopper_id INTEGER,              -- кто написал /стоп
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    status TEXT NOT NULL DEFAULT 'active',  -- active | finished | confirmed | rejected
    start_cmid INTEGER
);
CREATE TABLE IF NOT EXISTS participants(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    training_id INTEGER NOT NULL,
    num INTEGER NOT NULL,            -- постоянный номер в отчёте
    vk_id INTEGER,                   -- NULL, если добавлен вручную через «+»
    name TEXT NOT NULL,
    join_ts INTEGER NOT NULL,
    leave_ts INTEGER,
    join_cmid INTEGER,
    leave_cmid INTEGER,
    auto_leave INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reports(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    training_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    message_id INTEGER,
    cmid INTEGER,
    sent_ts INTEGER NOT NULL,
    note TEXT                        -- пояснение вверху отчёта (почему отчёт пришёл именно вам)
);
CREATE TABLE IF NOT EXISTS trainers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    training_id INTEGER NOT NULL,
    num INTEGER NOT NULL,            -- постоянный номер тренера в отчёте
    vk_id INTEGER,                   -- NULL — добавлен вручную без страницы ВК
    name TEXT,                       -- NULL — взять из листа «Участники» / из ВК
    join_ts INTEGER NOT NULL,
    leave_ts INTEGER,
    join_cmid INTEGER,
    leave_cmid INTEGER
);
CREATE TABLE IF NOT EXISTS msg_log(
    peer_id INTEGER NOT NULL,
    cmid INTEGER NOT NULL,
    from_id INTEGER,
    date INTEGER,
    text TEXT,                       -- последний известный текст сообщения
    recognized INTEGER NOT NULL DEFAULT 1,  -- 0 — команда не сработала: следим, не исправят ли
    added_ts INTEGER NOT NULL,
    PRIMARY KEY (peer_id, cmid)
);
CREATE TABLE IF NOT EXISTS settings_msgs(
    user_id INTEGER PRIMARY KEY,     -- кто вызвал /настройка
    cmid INTEGER,                    -- последнее сообщение с настройками (его редактируем)
    sent_ts INTEGER,
    text TEXT
);
CREATE TABLE IF NOT EXISTS chats(
    peer_id INTEGER PRIMARY KEY,
    title TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,      -- учёт баллов в беседе
    free_run INTEGER NOT NULL DEFAULT 0,     -- свободный бег (задел на будущее)
    admins_only INTEGER NOT NULL DEFAULT 0,  -- тренировки ведут только админы беседы
    added_ts INTEGER,
    self_gov_days TEXT NOT NULL DEFAULT ''   -- дни самоуправления: «0,2,5» (пн=0 … вс=6), пусто — отключено
);
"""
)


def ensure_column(table: str, col: str, ddl: str):
    """Миграция: добавляет столбец в таблицу, созданную более старой версией бота."""
    if col not in [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


ensure_column("chats", "self_gov_days", "TEXT NOT NULL DEFAULT ''")
ensure_column("reports", "note", "TEXT")
ensure_column("reports", "page", "INTEGER NOT NULL DEFAULT 0")
db.execute("CREATE TABLE IF NOT EXISTS bot_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
db.execute(
    "CREATE TABLE IF NOT EXISTS free_run_msgs(peer_id INTEGER NOT NULL, cmid INTEGER NOT NULL, from_id INTEGER, "
    "ts INTEGER NOT NULL, names TEXT NOT NULL, PRIMARY KEY(peer_id, cmid))"
)
ensure_column("participants", "deduct", "INTEGER NOT NULL DEFAULT 0")  # вычет минут по отписи (свободный бег)
ensure_column("participants", "deduct_cmid", "INTEGER")
ensure_column("participants", "flag", "TEXT")  # пометки для администратора
ensure_column("participants", "verified", "INTEGER NOT NULL DEFAULT 0")  # админ подтвердил правки бота
ensure_column("participants", "src", "TEXT")   # текст отписи, по которой записан участник
ensure_column("trainings", "touch_ts", "INTEGER")  # свободный бег: когда была последняя отпись
ensure_column("trainings", "kind", "TEXT NOT NULL DEFAULT 'group'")  # group | individual
ensure_column("trainings", "stop_cmid", "INTEGER")  # сообщение «/стоп» (если его удалят — тренировка продолжается)
ensure_column("msg_log", "checked_ts", "INTEGER NOT NULL DEFAULT 0")  # когда проверяли, не удалено ли сообщение
ensure_column("msg_log", "miss", "INTEGER NOT NULL DEFAULT 0")        # сколько проверок подряд его не нашли
ensure_column("msg_log", "gone", "INTEGER NOT NULL DEFAULT 0")        # 1 — удаление обработано
db.execute(
    "CREATE TABLE IF NOT EXISTS menu_state("
    "user_id INTEGER PRIMARY KEY, role TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS edit_sessions("
    "user_id INTEGER PRIMARY KEY, training_id INTEGER NOT NULL, kind TEXT NOT NULL, "
    "num INTEGER, cmid INTEGER, ts INTEGER NOT NULL)"
)
db.commit()

# ───────────────────────── утилиты ─────────────────────────
def floor_min(ts: int) -> int:
    return ts - ts % 60


def dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, MSK)


def resolve_clock(msg_ts: int, h, m, not_before: int = None):
    """
    «ЧЧ:ММ» из команды (/старт 10:00, /стоп 12:00) → момент времени. Берётся ближайшее к моменту
    отправки сообщения (вчера/сегодня/завтра), поэтому работает и для будущего времени (написали
    в 9:00 «/старт 10:00»), и для прошедшего, и через полночь. None — время некорректно.
    """
    h, m = int(h), int(m)
    if h > 23 or m > 59:
        return None
    d0 = dt(msg_ts).replace(hour=h, minute=m, second=0, microsecond=0)
    cands = [int((d0 + timedelta(days=k)).timestamp()) for k in (-1, 0, 1)]
    best = min(cands, key=lambda c: abs(c - msg_ts))
    if not_before is not None and best < not_before:
        return None  # например, «/стоп» со временем раньше старта — игнорируем указанное время
    return best


def norm_name(raw: str) -> str:
    """Убирает кличку в скобках и приводит к виду «Иван Петров»."""
    s = re.sub(r"\(.*", "", raw)
    s = re.sub(r"^[\s,.:;\-–—]+|[\s,.:;\-–—]+$", "", s)
    return " ".join(s.split()).title()


def calc_points(minutes: int) -> float:
    """
    До 75 минут шаг 25 минут, начиная с 75 — шаг 30 минут:
    0–24 → 0; 25–49 → 0,5; 50–74 → 1; 75–104 → 1,5; 105–134 → 2; 135–164 → 2,5 и т.д.
    """
    if minutes < 75:
        return (minutes // 25) * 0.5
    return 1.5 + ((minutes - 75) // 30) * 0.5


def fmt_points(p: float) -> str:
    return f"{p:g}".replace(".", ",")


def fmt_dur(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} ч {m} минут"
    if h:
        return f"{h} ч"
    return f"{m} минут"


def calc_free_points(minutes: int) -> float:
    """Свободный бег: <25 мин — 0; 25–49 — 0,5; 50–104 — 1; 105–134 — 1,5; затем +0,5 за каждые 30 минут."""
    if minutes < 25:
        return 0.0
    if minutes < 50:
        return 0.5
    if minutes < 105:
        return 1.0
    return 1.5 + ((minutes - 105) // 30) * 0.5


def minutes_of(p) -> int:
    return max(0, (p["leave_ts"] - p["join_ts"]) // 60 - (p["deduct"] or 0))


def fmt_t(ts: int, start_ts: int) -> str:
    """ЧЧ:ММ; если время выпало на другие сутки, чем старт тренировки, — добавляет (дд.мм)."""
    d = dt(ts)
    s = f"{d:%H:%M}"
    if d.date() != dt(start_ts).date():
        s += f" ({d:%d.%m})"
    return s


def training_label(t):
    """(дата, время): «24.10.2026», «15:00-16:30»; через полночь дата — «24.10.2026–25.10.2026»."""
    s, e = dt(t["start_ts"]), dt(t["end_ts"])
    date = f"{s:%d.%m.%Y}" if s.date() == e.date() else f"{s:%d.%m.%Y}–{e:%d.%m.%Y}"
    return date, f"{s:%H:%M}-{e:%H:%M}"


def name_key(s: str) -> str:
    """Имя для сравнения: без регистра, ё=е, лишние пробелы убраны."""
    return " ".join(str(s).casefold().replace("ё", "е").split())


_names = {}


def user_name(uid: int) -> str:
    if uid not in _names:
        try:
            u = vk.users.get(user_ids=uid)[0]
            _names[uid] = f"{u['first_name']} {u['last_name']}"
        except Exception as e:  # noqa: BLE001
            log.warning("users.get(%s) failed: %s", uid, e)
            _names[uid] = "Имя"
    return _names[uid]


def get_members():
    """Справочник из листа «Участники» (кэш внутри sheets.py) или None, если таблица недоступна."""
    try:
        import sheets
        return sheets.load_members()
    except Exception as e:  # noqa: BLE001
        log.warning("Лист «Участники» недоступен, CW ID не подставляются: %s", e)
        return None


def trainer_info(members, leader_id: int):
    """(имя для отчёта, CW ID, найден ли в таблице). Имя — «Имя на варе», иначе имя из ВК."""
    rec = members.trainer(leader_id) if members else None
    if rec:
        return rec["name"] or user_name(leader_id), rec["cw_id"], True
    return user_name(leader_id), "", False


def autocorrect_participants(tid: int):
    """
    Автокоррекция имён по листу «Участники»: точное совпадение → без знаков → по первым двум словам.
    Найденное имя заменяется на написание из таблицы. Не нашлось — имя остаётся как есть
    (в отчёте помечено ⚠, исправить должен человек: «N имя Верное Имя»).
    """
    members = get_members()
    if not members:
        return
    for p in db.execute("SELECT * FROM participants WHERE training_id=?", (tid,)).fetchall():
        rec = members.participant(p["name"], p["vk_id"])
        if rec and rec["name"] and rec["name"] != p["name"]:
            log.info("Автокоррекция имени: «%s» → «%s»", p["name"], rec["name"])
            db.execute("UPDATE participants SET name=? WHERE id=?", (rec["name"], p["id"]))
    db.commit()


def user_link(uid: int) -> str:
    return f"[id{uid}|{user_name(uid)}]"


def who(p) -> str:
    return f"[id{p['vk_id']}|{p['name']}]" if p["vk_id"] else p["name"]


def react(peer_id: int, cmid, key: str):
    rid = REACTIONS.get(key)
    if rid is None:
        log.warning("Не задан ID реакции '%s' — пропускаю", key)
        return
    if not cmid:
        return
    try:
        # повторный вызов заменяет прежнюю реакцию бота на этом сообщении
        vk.messages.sendReaction(peer_id=peer_id, cmid=cmid, reaction_id=rid)
    except ApiError as e:
        log.warning("sendReaction(%s,%s,%s): %s", peer_id, cmid, key, e)


_admin_checked = set()


_titles = {}


def chat_title(peer_id: int, refresh: bool = False) -> str:
    """Название беседы (с кэшем). Если получить не удалось — «Беседа №N»."""
    if refresh or peer_id not in _titles:
        try:
            r = vk.messages.getConversationsById(peer_ids=peer_id)
            _titles[peer_id] = r["items"][0]["chat_settings"]["title"]
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось получить название беседы %s: %s", peer_id, e)
            _titles.setdefault(peer_id, f"Беседа №{peer_id - 2_000_000_000}")
    return _titles[peer_id]


def ensure_admin(peer_id: int):
    """Один раз за запуск проверяем, что бот — админ беседы (только лог)."""
    if peer_id in _admin_checked:
        return
    _admin_checked.add(peer_id)
    try:
        r = vk.messages.getConversationMembers(peer_id=peer_id)
        for it in r.get("items", []):
            if it["member_id"] == -GROUP_ID:
                if not it.get("is_admin"):
                    log.warning("Бот НЕ админ в беседе %s", peer_id)
                return
        log.warning("Бота нет в списке участников беседы %s", peer_id)
    except ApiError as e:
        log.warning("Нет доступа к списку участников %s (скорее всего, нет прав админа): %s",
                    peer_id, e)


# ───────────────────────── беседы и права ─────────────────────────
_known_chats = set()


def get_chat(peer_id: int):
    return db.execute("SELECT * FROM chats WHERE peer_id=?", (peer_id,)).fetchone()


def register_chat(peer_id: int):
    """Запоминает беседу при первом сообщении (для списка в /настройка)."""
    if peer_id in _known_chats:
        return
    if not get_chat(peer_id):
        db.execute(
            "INSERT INTO chats(peer_id,title,enabled,added_ts) VALUES(?,?,?,?)",
            (peer_id, chat_title(peer_id, refresh=True), DEFAULT_CHAT_ENABLED, int(time.time())),
        )
        db.commit()
    _known_chats.add(peer_id)


def handle_action(peer_id: int, action: dict):
    """Служебные события беседы: добавление/исключение бота, переименование."""
    typ = action.get("type")
    if typ == "chat_invite_user" and action.get("member_id") == -GROUP_ID:
        register_chat(peer_id)
    elif typ == "chat_kick_user" and action.get("member_id") == -GROUP_ID:
        db.execute("DELETE FROM chats WHERE peer_id=?", (peer_id,))
        db.commit()
        _known_chats.discard(peer_id)
    elif typ == "chat_title_update" and action.get("text"):
        register_chat(peer_id)
        _titles[peer_id] = action["text"]
        db.execute("UPDATE chats SET title=? WHERE peer_id=?", (action["text"], peer_id))
        db.commit()


_chat_admins = {}  # peer_id -> (время загрузки, {VK ID админов беседы})


def _load_chat_admins(peer_id: int):
    ids = set()
    try:
        r = vk.messages.getConversationMembers(peer_id=peer_id)
        for it in r.get("items", []):
            if it.get("is_admin") or it.get("is_owner"):
                ids.add(it["member_id"])
    except ApiError as e:
        log.warning("Не удалось получить админов беседы %s: %s", peer_id, e)
    _chat_admins[peer_id] = (time.time(), ids)
    return ids


def is_chat_admin(peer_id: int, uid: int) -> bool:
    """Админ беседы? Список кэшируется на 2 минуты; при «нет» — обновляется не чаще раза в 20 секунд."""
    cached = _chat_admins.get(peer_id)
    if not cached or time.time() - cached[0] > 120:
        return uid in _load_chat_admins(peer_id)
    if uid in cached[1]:
        return True
    if time.time() - cached[0] > 20:
        return uid in _load_chat_admins(peer_id)
    return False


_group_roles = {"ts": 0.0, "roles": {}}
ADMIN_ROLES = ("creator", "administrator")
MODER_ROLES = ADMIN_ROLES + ("editor", "moderator")  # «модератор и выше»


def group_roles() -> dict:
    """{VK ID: роль} руководителей сообщества; кэш на 5 минут."""
    if time.time() - _group_roles["ts"] > 300:
        roles = {}
        try:
            r = vk.groups.getMembers(group_id=GROUP_ID, filter="managers")
            for it in r.get("items", []):
                if isinstance(it, dict):
                    roles[it["id"]] = it.get("role")
                else:
                    roles[it] = "administrator"
        except ApiError as e:
            log.warning("Не удалось получить руководителей сообщества: %s "
                        "(можно задать BOT_ADMIN_IDS в .env)", e)
        _group_roles.update(ts=time.time(), roles=roles)
    return _group_roles["roles"]


def group_admin_ids() -> set:
    """Создатель и администраторы сообщества (+ запасной список BOT_ADMIN_IDS)."""
    return {u for u, r in group_roles().items() if r in ADMIN_ROLES} | ENV_ADMIN_IDS


def group_moderator_ids() -> set:
    """Модераторы, редакторы, администраторы и создатель сообщества (+ BOT_ADMIN_IDS)."""
    return {u for u, r in group_roles().items() if r in MODER_ROLES} | ENV_ADMIN_IDS


def group_owner_ids() -> set:
    """Владелец (создатель) сообщества (+ запасной список BOT_OWNER_IDS)."""
    return {u for u, r in group_roles().items() if r == "creator"} | ENV_OWNER_IDS


def is_group_admin(uid: int) -> bool:
    return uid in group_admin_ids()


def is_group_moderator(uid: int) -> bool:
    return uid in group_moderator_ids()


# ───────────────────────── тренеры ─────────────────────────
def trainer_rows(tid: int):
    return db.execute("SELECT * FROM trainers WHERE training_id=? ORDER BY num", (tid,)).fetchall()


def ensure_trainers(t):
    """Тренировки, начатые старой версией бота: единственный тренер — тот, кто написал /старт."""
    if t["kind"] == "free_run":
        return  # в свободном беге тренеров нет
    if db.execute("SELECT 1 FROM trainers WHERE training_id=?", (t["id"],)).fetchone():
        return
    db.execute(
        "INSERT INTO trainers(training_id,num,vk_id,join_ts,leave_ts,join_cmid) VALUES(?,?,?,?,?,?)",
        (t["id"], 1, t["leader_id"], t["start_ts"], t["end_ts"], t["start_cmid"]),
    )
    db.commit()


def trainer_view(r, members):
    """(имя для отчёта, CW ID, найден ли на листе «Участники»)."""
    rec = None
    if members:
        if r["vk_id"]:
            rec = members.trainer(r["vk_id"])
        elif r["name"]:
            rec = members.participant(r["name"])
    if r["name"]:
        name = r["name"]
    elif rec and rec["name"]:
        name = rec["name"]
    elif r["vk_id"]:
        name = user_name(r["vk_id"])
    else:
        name = "Тренер"
    return name, (rec["cw_id"] if rec else ""), bool(rec)


def trainer_link(r, name: str) -> str:
    return f"[id{r['vk_id']}|{name}]" if r["vk_id"] else name


# ───────────────────────── Google Таблица ─────────────────────────
def training_chat_title(t) -> str:
    return "Индивидуальная тренировка" if t["kind"] == "individual" else chat_title(t["peer_id"])


def build_free_run_payload(t, members) -> dict:
    """Данные забега для sheets.write_free_run(): у каждого участника свои время прихода и ухода."""
    parts = []
    for p in db.execute("SELECT * FROM participants WHERE training_id=? ORDER BY num", (t["id"],)):
        mins = minutes_of(p)
        rec = members.participant(p["name"], p["vk_id"]) if members else None
        parts.append({
            "vk_id": p["vk_id"], "cw_id": rec["cw_id"] if rec else "",
            "name": (rec["name"] if rec and rec["name"] else p["name"]),
            "join": dt(p["join_ts"]), "leave": dt(p["leave_ts"]), "minutes": mins,
            "points": calc_free_points(mins), "deduct": p["deduct"] or 0, "note": p["flag"] or "",
        })
    return {"key": f"fr-{t['peer_id']}-{t['start_ts']}-{t['id']}", "start": dt(t["start_ts"]),
            "end": dt(t["end_ts"]), "chat_id": t["peer_id"] - 2_000_000_000,
            "chat_title": chat_title(t["peer_id"]), "participants": parts}


def build_training_payload(training_id: int) -> dict:
    """
    Данные тренировки для sheets.write_training(). CW ID тренера берётся с листа «Участники»
    по VK ID («ID вк» → «ID вар»), CW ID участника — по имени («Имя на варе»).
    Если тренеров несколько, их данные записываются в одну ячейку через запятую.
    """
    t = db.execute("SELECT * FROM trainings WHERE id=?", (training_id,)).fetchone()
    members = get_members()
    if t["kind"] == "free_run":
        return build_free_run_payload(t, members)
    ensure_trainers(t)
    trs = trainer_rows(training_id)
    views = [(r, *trainer_view(r, members)) for r in trs]
    parts = []
    for p in db.execute(
        "SELECT * FROM participants WHERE training_id=? ORDER BY num", (training_id,)
    ):
        mins = minutes_of(p)
        rec = members.participant(p["name"], p["vk_id"]) if members else None
        parts.append({
            "vk_id": p["vk_id"],
            "cw_id": rec["cw_id"] if rec else "",
            "name": (rec["name"] if rec and rec["name"] else p["name"]),  # написание как на листе «Участники»
            "join": dt(p["join_ts"]), "leave": dt(p["leave_ts"]),
            "minutes": mins, "points": calc_points(mins), "auto_leave": bool(p["auto_leave"]),
        })
    ids = [r["vk_id"] for r, _, _, _ in views if r["vk_id"]] or [t["leader_id"]]
    names = [n for _, n, _, _ in views] or [user_name(t["leader_id"])]
    cws = [c for _, _, c, _ in views if c != ""]
    if len(views) <= 1:
        leader_id, leader_name, leader_cw = ids[0], names[0], (cws[0] if cws else "")
    else:
        leader_id, leader_name = ", ".join(map(str, ids)), ", ".join(names)
        leader_cw = ", ".join(map(str, cws))
    return {
        "key": (f"ind-{t['peer_id']}-{t['start_ts']}-{t['id']}" if t["kind"] == "individual"
                else f"{t['peer_id']}-{t['start_ts']}"),
        "start": dt(t["start_ts"]), "end": dt(t["end_ts"]),
        "chat_id": 0 if t["kind"] == "individual" else t["peer_id"] - 2_000_000_000,
        "chat_title": training_chat_title(t),
        "leader_id": leader_id, "leader_name": leader_name, "leader_cw_id": leader_cw,
        "participants": parts,
    }


def write_to_google_sheet(training_id: int) -> bool:
    """Записывает тренировку в Google Таблицу. True — успех (в т.ч. если уже была записана)."""
    try:
        import sheets
    except ImportError as e:
        log.error("Модуль sheets.py недоступен: %s", e)
        return False
    try:
        payload = build_training_payload(training_id)
        kind = db.execute("SELECT kind FROM trainings WHERE id=?", (training_id,)).fetchone()["kind"]
        res = sheets.write_free_run(payload) if kind == "free_run" else sheets.write_training(payload)
        log.info("Запись в таблицу, тренировка %s: %s", training_id, res)
        return True
    except sheets.SheetsError as e:
        log.error("Запись в таблицу не удалась: %s", e)
        return False


# ───────────────────────── отчёт ─────────────────────────
REPORT_HELP = ("Если что-то неверно, исправь отчёт кнопками ниже. Когда всё будет верно, нажми «Подтвердить». "
               "«Отклонить» — отчёт не будет записан в таблицу.")

# Пометки свободного бега делятся на группы (порядок = порядок вывода). Первая группа — то, что обязательно
# должен проверить администратор; остальные — автоматические исправления бота, о которых он сообщает.
FLAG_ORDER = (
    ("manual", "🚨", "Требует правки",
     re.compile(r"укажите|проверьте|неоднозначно|раньше времени прихода|несуществующее|не понятно, к чему|"
                r"не применил|имя не указано", re.I)),
    ("name", "🔤", "Имя / написание", re.compile(r"распознано как|раскладк|выбрано по недавним", re.I)),
    ("time", "🕒", "Время", re.compile(r"^время|минут назад|сдвинуто|^«через", re.I)),
    ("deduct", "➖", "Вычет", re.compile(r"^вычет", re.I)),
    ("other", "ℹ️", "На заметку", None),
)
FLAG_LEGEND = {
    "manual": "🚨 — нужна ручная правка администратора",
    "name": "🔤 — имя или слово исправлено автоматически",
    "time": "🕒 — время понято/пересчитано автоматически",
    "deduct": "➖ — вычет минут по отписи",
    "other": "ℹ️ — на заметку",
}


def render_flags(flag_text: str):
    """Пометки участника → (строки отчёта по группам, есть ли требующие ручной правки, множество групп)."""
    buckets = {}
    for f in (x.strip() for x in (flag_text or "").split("; ")):
        if not f:
            continue
        key = next((g[0] for g in FLAG_ORDER if g[3] is not None and g[3].search(f)), "other")
        buckets.setdefault(key, []).append(f)
    lines = [f"{emoji} {label}: {'; '.join(buckets[key])}"
             for key, emoji, label, _ in FLAG_ORDER if key in buckets]
    return lines, "manual" in buckets, set(buckets)


def render_report(tid: int, note: str = None, confirmed_by: str = None, rejected_by: str = None,
                  void_reason: str = None) -> str:
    """
    note — пояснение, почему отчёт пришёл именно этому получателю;
    confirmed_by — ссылка на того, кто подтвердил запись (дописывается вверху);
    rejected_by — ссылка на того, кто отклонил отчёт (дописывается вверху).
    """
    t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    ensure_trainers(t)
    parts = db.execute(
        "SELECT * FROM participants WHERE training_id=? ORDER BY num", (tid,)
    ).fetchall()

    members = get_members()
    unmatched = []
    trainer_warn = [False]
    manual_nums, used_groups = set(), set()  # свободный бег: кому нужна ручная правка / какие группы пометок были

    fr = t["kind"] == "free_run"
    mark = "🚨" if fr else "⚠"

    def spaced(items):
        """Свободный бег: пустая строка перед каждым следующим участником (после пометок предыдущего)."""
        res = []
        for i, x in enumerate(items):
            if i and fr:
                res.append("")
            res.append(x)
        return res

    def line(p):
        m = minutes_of(p)
        warn = ""
        if members and not (fr and p["verified"]) and not members.participant(p["name"], p["vk_id"]):
            warn = f" {mark}"
            unmatched.append(p["num"])
            manual_nums.add(p["num"])
        if fr:
            ded = f" (с вычетом {p['deduct']} мин)" if p["deduct"] else ""
            res = (f"{p['num']}. {who(p)}{warn} {fmt_t(p['join_ts'], t['start_ts'])} - "
                   f"{fmt_t(p['leave_ts'], t['start_ts'])}, {fmt_dur(m)}{ded}, "
                   f"{fmt_points(calc_free_points(m))} б.")
            if m < 25:
                res += " (меньше 25 минут — не засчитывается)"
            if p["flag"]:
                fl_lines, fl_manual, fl_groups = render_flags(p["flag"])
                res += "".join("\n    " + x for x in fl_lines)
                used_groups.update(fl_groups)
                if fl_manual:
                    manual_nums.add(p["num"])
            if p["src"]:
                res += f"\n    «{p['src'][:150]}»"
            return res
        return (
            f"{p['num']}. {who(p)}{warn} {fmt_t(p['join_ts'], t['start_ts'])} - "
            f"{fmt_t(p['leave_ts'], t['start_ts'])}, "
            f"{fmt_dur(m)}, {fmt_points(calc_points(m))} б."
        )

    d_part, t_part = training_label(t)
    indiv = t["kind"] == "individual"
    head = ("Свободный бег " if fr else "Индивидуальная тренировка " if indiv else "Тренировка ") + f"{d_part}. {t_part}"
    # «Участники» — кто не уходил раньше конца тренировки; «Ушли досрочно» — остальные
    stayed = [line(p) for p in parts if fr or p["leave_ts"] >= t["end_ts"]]
    early = [line(p) for p in parts if not fr and p["leave_ts"] < t["end_ts"]]

    trs = trainer_rows(tid)
    views = [(r, *trainer_view(r, members)) for r in trs]
    if members and any(not found for _, _, _, found in views):
        trainer_warn[0] = True
    if len(trs) == 1 and trs[0]["join_ts"] <= t["start_ts"] and (trs[0]["leave_ts"] or 0) >= t["end_ts"]:
        r, name, _, _ = views[0]
        trainer_lines = [f"Вёл тренировку: {trainer_link(r, name)}"]
    else:
        trainer_lines = ["Тренеры:"]
        for r, name, _, found in views:
            warn = " ⚠" if members and not found else ""
            trainer_lines.append(
                f"{r['num']}. {trainer_link(r, name)}{warn} {fmt_t(r['join_ts'], t['start_ts'])} - "
                f"{fmt_t(r['leave_ts'] or t['end_ts'], t['start_ts'])}"
            )

    out = []
    if confirmed_by:
        out += [f"✔ Запись подтвердил(а): {confirmed_by}", ""]
    if rejected_by:
        out += [f"✖ Отчёт отклонил(а): {rejected_by} — в таблицу не записан", ""]
    if void_reason:
        out += [f"✖ Отчёт отменён: {void_reason}. В таблицу он не записан", ""]
    if note:
        out += [f"ℹ {note}", ""]
    if fr and manual_nums and not (confirmed_by or rejected_by or void_reason):
        out += ["🚨 Требуют ручной проверки: " + ", ".join(f"№{n}" for n in sorted(manual_nums)), ""]
    out += [
        head,
        *([] if indiv else [f"Беседа: {chat_title(t['peer_id'])}"]),
        "",
        *([] if fr else [*trainer_lines, ""]),
        "Участники:",
        *(spaced(stayed) or ["—"]),
    ]
    if early:
        out += ["", "Ушли досрочно:", *early]
    notes = []
    if unmatched:
        notes.append(f"{mark} — персонаж не найден на листе «Участники», CW ID не будет записан. "
                     "Если имя написано с ошибкой, нажми на участника и исправь имя.")
    if trainer_warn[0]:
        notes.append("⚠ Тренер не найден на листе «Участники» по VK ID — CW ID тренера останется пустым.")
    if fr and manual_nums:
        used_groups.add("manual")
    if fr and used_groups and not (confirmed_by or rejected_by or void_reason):
        notes.append("Пометки: " + "; ".join(FLAG_LEGEND[k] for k, *_ in FLAG_ORDER if k in used_groups))
    if notes:
        out += ["", *notes]
    if not (confirmed_by or rejected_by or void_reason):
        out += ["", REPORT_HELP]
    return "\n".join(out)


REPORT_PAGE = 4  # записей на страницу (inline-клавиатура VK: ≤ 6 строк и ≤ 10 кнопок: 4 + ◀/Добавить/▶ + время + 2 в последнем ряду)


def _entry_label(prefix: str, num: int, name: str, j: int, l: int, warn: bool) -> str:
    mark = warn if isinstance(warn, str) else ("⚠" if warn else "")
    head = (mark + " " if mark else "") + f"{prefix}{num}. "
    tail = f" {dt(j):%H:%M}-{dt(l):%H:%M}"
    room = 40 - len(head) - len(tail)  # у кнопки VK не больше 40 символов
    if len(name) > room:
        name = name[:max(room - 1, 1)] + "…"
    return head + name + tail


def report_entries(tid: int):
    """Записи для кнопок: тренеры (если их больше одного), затем участники."""
    t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    ensure_trainers(t)
    members = get_members()
    out = []
    trs = trainer_rows(tid)
    if len(trs) > 1:
        for r in trs:
            name, _, found = trainer_view(r, members)
            out.append({"k": "t", "n": r["num"], "label": _entry_label(
                "Тр. ", r["num"], name, r["join_ts"], r["leave_ts"] or t["end_ts"], bool(members) and not found)})
    for p in db.execute("SELECT * FROM participants WHERE training_id=? ORDER BY num", (tid,)):
        if t["kind"] == "free_run":  # 🚨 — нужна ручная правка, ℹ️ — только автоправки бота
            warn = entry_mark(t, p, members)
        else:
            warn = bool(members) and not members.participant(p["name"], p["vk_id"])
        out.append({"k": "p", "n": p["num"], "label": _entry_label(
            "", p["num"], p["name"], p["join_ts"], p["leave_ts"] or t["end_ts"], warn)})
    return out


def confirm_kb(tid: int, page: int = 0) -> str:
    """Кнопки правок (по 4 записи на странице) + «Добавить участника» + «Подтвердить» / «Отклонить» (всегда последние)."""
    entries = report_entries(tid)
    pages = max(1, -(-len(entries) // REPORT_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = entries[page * REPORT_PAGE:(page + 1) * REPORT_PAGE]
    sec = VkKeyboardColor.SECONDARY
    kb = VkKeyboard(inline=True)
    for i, e in enumerate(chunk):
        if i and i % 2 == 0:
            kb.add_line()
        kb.add_callback_button(e["label"], sec, payload={"cmd": "rp_sel", "tid": tid, "k": e["k"], "n": e["n"]})
    if chunk:
        kb.add_line()
    if page > 0:
        kb.add_callback_button("◀", sec, payload={"cmd": "rp_page", "tid": tid, "pg": page - 1})
    kb.add_callback_button("Добавить участника", sec, payload={"cmd": "rp_add", "tid": tid})
    if page < pages - 1:
        kb.add_callback_button("▶", sec, payload={"cmd": "rp_page", "tid": tid, "pg": page + 1})
    kb.add_line()
    if not db.execute("SELECT 1 FROM trainings WHERE id=? AND kind='free_run'", (tid,)).fetchone():
        kb.add_callback_button("Изменить время тренировки", sec, payload={"cmd": "rp_time", "tid": tid})
        kb.add_line()
    kb.add_callback_button(
        "Подтвердить", VkKeyboardColor.POSITIVE, payload={"cmd": "confirm", "tid": tid}
    )
    kb.add_callback_button(
        "Отклонить", VkKeyboardColor.NEGATIVE, payload={"cmd": "rp_rej", "tid": tid}
    )
    return kb.get_keyboard()


REJECT_WARN = ("❗ Отклонить отчёт? В таблицу он записан не будет, кнопки пропадут у всех получателей. "
               "Отменить это действие будет нельзя.")


def reject_kb(tid: int) -> str:
    """Повторное подтверждение отклонения."""
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("Да, отклонить", VkKeyboardColor.NEGATIVE, payload={"cmd": "rp_rej_yes", "tid": tid})
    kb.add_callback_button("Отмена", VkKeyboardColor.SECONDARY, payload={"cmd": "rp_rej_no", "tid": tid})
    return kb.get_keyboard()


def send_report_message(tid: int, uid: int, note: str = None) -> bool:
    """Отправляет отчёт одному получателю. False — доставить нельзя (запретил сообщения, не писал и т.п.)."""
    try:
        r = vk.messages.send(
            peer_ids=uid,
            random_id=random.getrandbits(31),
            message=render_report(tid, note=note),
            keyboard=confirm_kb(tid, 0),
            disable_mentions=1,
        )
    except ApiError as e:
        log.warning("Не удалось отправить отчёт %s: %s", uid, e)
        return False
    item = r[0] if r else {}
    if item.get("error"):
        log.warning("Отчёт для %s не доставлен: %s (нужно разрешить сообщения от сообщества)",
                    uid, item["error"])
        return False
    db.execute(
        "INSERT INTO reports(training_id,user_id,message_id,cmid,sent_ts,note) VALUES(?,?,?,?,?,?)",
        (tid, uid, item.get("message_id"), item.get("conversation_message_id"), int(time.time()), note),
    )
    db.commit()
    return True


def deliver_report(tid: int, uids, note: str = None):
    """Рассылает отчёт всем из списка (без повторов). Возвращает тех, кому доставлено."""
    done = []
    for uid in dict.fromkeys(u for u in uids if u and u > 0):
        if send_report_message(tid, uid, note):
            done.append(uid)
    return done


def dispatch_report(tid: int):
    """
    Кому отправить отчёт после /стоп:
      • обычно — всем участвовавшим тренерам;
      • в день самоуправления, если тренировку провёл(и) не администратор(ы) беседы — администраторам сообщества;
      • если тренерам доставить нельзя — резервно администраторам сообщества (подтвердить может любой из них).
    """
    t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    if t["kind"] == "free_run":  # тренеров нет: отчёт получают все администраторы сообщества
        note = ("Свободный бег: отчёт отправлен всем администраторам сообщества, "
                "подтвердить может любой из вас.")
        if not deliver_report(tid, sorted(group_admin_ids()), note):
            log.error("Отчёт по свободному бегу %s не удалось доставить никому", tid)
        return
    ensure_trainers(t)
    chat = get_chat(t["peer_id"])
    trainers = [r["vk_id"] for r in trainer_rows(tid) if r["vk_id"] and r["vk_id"] > 0]
    stopper = [t["stopper_id"]] if t["stopper_id"] else []
    admins = sorted(group_admin_ids())

    self_gov_no_admin = bool(chat) and is_self_gov_day(chat, t["start_ts"]) and not any(
        is_chat_admin(t["peer_id"], u) for u in trainers)
    if self_gov_no_admin:
        note = ("Отчёт отправлен вам как администратору сообщества: в день самоуправления "
                "тренировку провёл(и) не администратор беседы.")
        if deliver_report(tid, admins, note):
            return
        log.warning("Тренировка %s: администраторам сообщества отчёт доставить не удалось — пробую тренерам", tid)
        if deliver_report(tid, trainers + stopper):
            return
    else:
        if deliver_report(tid, trainers):
            return
        if deliver_report(tid, [u for u in stopper if u not in trainers]):
            return
        who_txt = ", ".join(user_link(u) for u in trainers) or "тренеру"
        note = f"Отчёт отправлен вам как администратору сообщества: не удалось доставить его тренеру ({who_txt})."
        if deliver_report(tid, [a for a in admins if a not in trainers], note):
            return
    log.error("Отчёт по тренировке %s не удалось доставить никому", tid)


def latest_reports(tid: int):
    """Последнее отправленное сообщение с отчётом для каждого получателя."""
    return db.execute(
        "SELECT * FROM reports WHERE training_id=? AND id IN "
        "(SELECT MAX(id) FROM reports WHERE training_id=? GROUP BY user_id) ORDER BY id",
        (tid, tid),
    ).fetchall()


def update_report(rep):
    tid, uid = rep["training_id"], rep["user_id"]
    if time.time() - rep["sent_ts"] < EDIT_LIMIT_SEC:
        try:
            vk.messages.edit(
                peer_id=uid,
                conversation_message_id=rep["cmid"],
                message=render_report(tid, note=rep["note"]),
                keyboard=confirm_kb(tid, rep["page"] or 0),
                disable_mentions=1,
            )
            return
        except ApiError as e:
            log.info("Редактирование недоступно (%s) — шлю новое сообщение", e)
    send_report_message(tid, uid, rep["note"])  # новое сообщение становится «исходным»


def refresh_run_bounds(tid: int):
    """Свободный бег: границы забега = от первого прихода до последнего ухода (после правок времени)."""
    t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    if not t or t["kind"] != "free_run" or t["status"] == "run":
        return
    r = db.execute("SELECT MIN(join_ts) a, MAX(COALESCE(leave_ts, join_ts)) b FROM participants "
                   "WHERE training_id=?", (tid,)).fetchone()
    if r["a"] is not None:
        db.execute("UPDATE trainings SET start_ts=?, end_ts=? WHERE id=?", (r["a"], r["b"], tid))
        db.commit()


def update_all_reports(tid: int):
    """После правок отчёта обновляет сообщения у ВСЕХ получателей (тренеров / администраторов)."""
    refresh_run_bounds(tid)
    for rep in latest_reports(tid):
        update_report(rep)


# ───────────────────────── обработка бесед ─────────────────────────
MENTION_RE = re.compile(r"^\s*\[club\d+\|[^\]]*\]\s*,?\s*", re.I)
# команда может стоять в любом месте сообщения, рядом может быть другой текст
# после команды можно указать время: «/старт 10:00», «/стоп в 12:00»
CLOCK = r"(?:[ \t]+(?:в[ \t]+)?(\d{1,2})[:.](\d{2})(?!\d))?"
RE_START = re.compile(r"(?<![\w/])/старт(?!\w)" + CLOCK, re.I)
RE_STOP = re.compile(r"(?<![\w/])/стоп(?!\w)" + CLOCK, re.I)
# команда может стоять после другого текста: берётся только то, что написано ПОСЛЕ неё
RE_JOIN = re.compile(r"(?<![\w#])#лапа(?!\w)\s+(.+)$", re.I | re.S)
RE_LEAVE = re.compile(r"(?<![\w#])#уход(?!\w)[ \t]*(.*)$", re.I | re.S)  # без имени — уходит тренер
RE_ADD_TRAINER = re.compile(r"^\+\s*тренер(?!\w)", re.I)  # «+ тренер» — стать тренером активной тренировки

RETRO_STOP_WINDOW_SEC = 2 * 3600   # правка «/старт»: если за 2 часа после него есть «/стоп» — не стартуем
EDIT_WATCH_SEC = 6 * 3600          # сколько часов следить за нераспознанными командами (опрос правок)
MSG_LOG_KEEP_SEC = 3 * 86400       # сколько хранить журнал команд (нужен для задним числом «/старт»)


def active_training(peer_id: int):
    return db.execute(
        "SELECT * FROM trainings WHERE peer_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (peer_id,),
    ).fetchone()


def next_num(tid: int) -> int:
    r = db.execute("SELECT COALESCE(MAX(num),0)+1 AS n FROM participants WHERE training_id=?", (tid,))
    return r.fetchone()["n"]


def next_trainer_num(tid: int) -> int:
    r = db.execute("SELECT COALESCE(MAX(num),0)+1 AS n FROM trainers WHERE training_id=?", (tid,))
    return r.fetchone()["n"]


def create_training(peer: int, uid: int, start_ts: int, cmid):
    """Новая тренировка; написавший /старт становится первым тренером."""
    cur = db.execute(
        "INSERT INTO trainings(peer_id,leader_id,start_ts,start_cmid) VALUES(?,?,?,?)",
        (peer, uid, start_ts, cmid),
    )
    db.execute(
        "INSERT INTO trainers(training_id,num,vk_id,join_ts,join_cmid) VALUES(?,?,?,?,?)",
        (cur.lastrowid, 1, uid, start_ts, cmid),
    )
    db.commit()
    return db.execute("SELECT * FROM trainings WHERE id=?", (cur.lastrowid,)).fetchone()


def active_trainer(tid: int, uid: int):
    return db.execute(
        "SELECT * FROM trainers WHERE training_id=? AND vk_id=? AND leave_ts IS NULL ORDER BY id LIMIT 1",
        (tid, uid),
    ).fetchone()


# ── дни самоуправления и права на проведение тренировок
DAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def self_gov_set(chat) -> set:
    return {int(x) for x in (chat["self_gov_days"] or "").split(",") if x.strip().isdigit()}


def is_self_gov_day(chat, ts: int) -> bool:
    """В день самоуправления вести тренировки может любой участник беседы."""
    return dt(ts).weekday() in self_gov_set(chat)


def can_train(chat, peer: int, uid: int, ts: int) -> bool:
    """Может ли uid начинать/вести тренировку (ts — время тренировки, нужно для дня самоуправления)."""
    if not chat["admins_only"] or is_self_gov_day(chat, ts):
        return True
    return is_chat_admin(peer, uid)


def process_join(peer: int, tr, uid: int, cmid, name: str, ts: int):
    """
    Регистрация участника по #лапа. Используется и для новых, и для отредактированных сообщений.
    Если это сообщение уже добавило участника (повторное событие или правка имени), дубль не создаётся:
    имя обновляется, пока участник не ушёл.
    """
    existing = db.execute(
        "SELECT * FROM participants WHERE training_id=? AND join_cmid=? AND vk_id=?",
        (tr["id"], cmid, uid),
    ).fetchone() if cmid else None
    if existing:
        if existing["leave_ts"] is None and existing["name"] != name:
            db.execute("UPDATE participants SET name=? WHERE id=?", (name, existing["id"]))
            db.commit()
        return
    dup = db.execute(
        "SELECT 1 FROM participants WHERE training_id=? AND vk_id=? AND name=? AND leave_ts IS NULL",
        (tr["id"], uid, name),
    ).fetchone()
    if dup:
        return
    db.execute(
        "INSERT INTO participants(training_id,num,vk_id,name,join_ts,join_cmid) VALUES(?,?,?,?,?,?)",
        (tr["id"], next_num(tr["id"]), uid, name, max(ts, tr["start_ts"]), cmid),  # раньше старта — время старта
    )
    db.commit()
    react(peer, cmid, "eye")


def classify(text: str):
    """Какая команда в тексте: start / join / leave / add_trainer / stop (порядок важен) или None."""
    if RE_START.search(text):
        return "start"
    if RE_JOIN.search(text):
        return "join"
    if RE_LEAVE.search(text):
        return "leave"
    if RE_ADD_TRAINER.match(text):
        return "add_trainer"
    if RE_STOP.search(text):
        return "stop"
    return None


def process_member_command(peer: int, chat, tr, uid: int, cmid, text: str, ts: int, kind: str) -> bool:
    """#лапа, #уход, «+ тренер» в активной тренировке. True — команда дала эффект (или осознанно проигнорирована)."""
    ensure_trainers(tr)
    if kind == "join":
        name = norm_name(RE_JOIN.search(text).group(1))
        if not name:
            return False
        process_join(peer, tr, uid, cmid, name, ts)
        return True

    if kind == "leave":
        m = RE_LEAVE.search(text)
        name = norm_name(m.group(1)) if m else ""
        p = None
        if name:
            base = "SELECT * FROM participants WHERE training_id=? AND leave_ts IS NULL "
            p = db.execute(base + "AND vk_id=? AND name=? ORDER BY id LIMIT 1", (tr["id"], uid, name)).fetchone()
            if not p:  # ушёл другой человек, а написал, например, ведущий
                p = db.execute(base + "AND name=? ORDER BY id LIMIT 1", (tr["id"], name)).fetchone()
            if not p:  # имя написано иначе, но у отправителя ровно одна активная запись
                rows = db.execute(base + "AND vk_id=?", (tr["id"], uid)).fetchall()
                p = rows[0] if len(rows) == 1 else None
        if p:
            db.execute(
                "UPDATE participants SET leave_ts=?, leave_cmid=? WHERE id=?",
                (max(ts, p["join_ts"]), cmid, p["id"]),
            )
            db.commit()
            react(peer, cmid, "check")
            react(peer, p["join_cmid"], "check")  # 👁 → ✔ на сообщении #лапа
            return True
        trn = active_trainer(tr["id"], uid)  # «#уход» тренера: покидает тренировку как тренер
        if trn:
            db.execute("UPDATE trainers SET leave_ts=?, leave_cmid=? WHERE id=?",
                       (max(ts, trn["join_ts"]), cmid, trn["id"]))
            db.commit()
            react(peer, cmid, "check")
            return True
        return False

    if kind == "add_trainer":
        if not can_train(chat, peer, uid, tr["start_ts"]):
            return True  # по правилам беседы стать тренером нельзя
        if active_trainer(tr["id"], uid):
            return True
        if cmid and db.execute("SELECT 1 FROM trainers WHERE training_id=? AND join_cmid=?",
                               (tr["id"], cmid)).fetchone():
            return True  # это сообщение уже добавило тренера (повторное событие)
        db.execute(
            "INSERT INTO trainers(training_id,num,vk_id,join_ts,join_cmid) VALUES(?,?,?,?,?)",
            (tr["id"], next_trainer_num(tr["id"]), uid, max(ts, tr["start_ts"]), cmid),
        )
        db.commit()
        react(peer, cmid, "up")
        return True
    return False


def stop_training(peer: int, chat, tr, uid: int, cmid, text: str, ts: int):
    """/стоп [время]: заканчивает тренировку и рассылает отчёт."""
    ensure_trainers(tr)
    if not (can_train(chat, peer, uid, tr["start_ts"]) or active_trainer(tr["id"], uid)):
        return  # завершить может тот, кто вправе вести тренировки, или тренер этой тренировки
    mst = RE_STOP.search(text)
    end = ts
    if mst.group(1):  # «/стоп 12:00» — конец тренировки в 12:00, даже если команда пришла позже
        end = resolve_clock(ts, mst.group(1), mst.group(2), not_before=tr["start_ts"]) or ts
    end = max(end, tr["start_ts"])
    # всё, что было «после конца» (вход/уход, записанные до получения /стоп), подтягиваем к концу
    for table in ("participants", "trainers"):
        db.execute(f"UPDATE {table} SET join_ts=? WHERE training_id=? AND join_ts>?", (end, tr["id"], end))
        db.execute(f"UPDATE {table} SET leave_ts=? WHERE training_id=? AND leave_ts>?", (end, tr["id"], end))
    # кто не ушёл сам — уходит в момент окончания тренировки
    db.execute(
        "UPDATE participants SET leave_ts=?, auto_leave=1 WHERE training_id=? AND leave_ts IS NULL",
        (end, tr["id"]),
    )
    db.execute("UPDATE trainers SET leave_ts=? WHERE training_id=? AND leave_ts IS NULL", (end, tr["id"]))
    db.execute(
        "UPDATE trainings SET status='finished', end_ts=?, stopper_id=?, stop_cmid=? WHERE id=?",
        (end, uid, cmid, tr["id"]),
    )
    db.commit()
    chat_title(peer, refresh=True)  # актуальное название на момент отчёта
    autocorrect_participants(tr["id"])
    dispatch_report(tr["id"])


def replay_log(peer: int, chat, tr):
    """
    Тренировка начата «задним числом» (исправленным /старт): применяем #лапа, #уход и «+ тренер»,
    написанные после этого сообщения, со временем их отправки.
    """
    rows = db.execute("SELECT * FROM msg_log WHERE peer_id=? AND cmid>? AND gone=0 ORDER BY cmid",
                      (peer, tr["start_cmid"] or 0)).fetchall()
    for r in rows:
        text = MENTION_RE.sub("", r["text"] or "").strip()
        kind = classify(text)
        if kind in ("join", "leave", "add_trainer") and r["from_id"] and r["from_id"] > 0:
            cur = active_training(peer)
            if not cur or cur["id"] != tr["id"]:
                break
            process_member_command(peer, chat, cur, r["from_id"], r["cmid"], text,
                                   floor_min(r["date"] or int(time.time())), kind)


def process_chat_command(peer: int, chat, uid: int, cmid, text: str, ts: int, edited: bool = False) -> bool:
    """
    Разбор команды из сообщения беседы (новое или отредактированное). ts — время отправки сообщения.
    True — команда распознана и отработана (или осознанно проигнорирована); False — эффекта не было
    (такое сообщение бот перечитывает: вдруг его исправят).
    """
    kind = classify(text)
    if kind is None:
        return False
    tr = active_training(peer)

    if kind == "start":
        if tr:
            return True
        ms = RE_START.search(text)
        # «/старт 10:00» — тренировка считается начатой в 10:00, даже если команда отправлена раньше/позже
        start_ts = (resolve_clock(ts, ms.group(1), ms.group(2)) if ms.group(1) else None) or ts
        if not can_train(chat, peer, uid, start_ts):
            return True  # в беседе тренировки ведут только администраторы
        if edited:
            # исправили сообщение: стартуем задним числом, только если в ближайшие 2 часа нет «/стоп»
            for r in db.execute("SELECT text FROM msg_log WHERE peer_id=? AND cmid>? AND date>=? AND date<=? AND gone=0",
                                (peer, cmid or 0, ts, ts + RETRO_STOP_WINDOW_SEC)):
                if RE_STOP.search(MENTION_RE.sub("", r["text"] or "").strip()):
                    log.info("Правка /старт (беседа %s, сообщение %s): в ближайшие 2 часа есть /стоп — "
                             "тренировку не начинаю", peer, cmid)
                    return True
        new = create_training(peer, uid, start_ts, cmid)
        react(peer, cmid, "up")
        if edited:
            log.info("Тренировка начата задним числом по исправленному /старт (беседа %s, сообщение %s)", peer, cmid)
            replay_log(peer, chat, new)
        return True

    if not tr:
        return kind == "stop"  # /стоп без тренировки — нечего заканчивать; остальные команды ничего не дали
    if edited and tr["start_cmid"] and cmid and cmid < tr["start_cmid"]:
        log.info("Команда из сообщения %s отправлена до /старт текущей тренировки — пропущена", cmid)
        return True
    if kind == "stop":
        stop_training(peer, chat, tr, uid, cmid, text, ts)
        return True
    return process_member_command(peer, chat, tr, uid, cmid, text, ts, kind)


def is_command_like(text: str) -> bool:
    return "/" in text or "#" in text or text.startswith("+")


def worth_watching(text: str) -> bool:
    """Стоит ли перечитывать нераспознанное сообщение: похоже на неудавшуюся команду."""
    return (text.startswith(("#", "/")) or bool(re.search(r"(?<![\w#])#(?:лапа|уход|бег)(?!\w)", text, re.I))
            or bool(re.match(r"\+\s*т", text, re.I)))


def log_message(peer: int, uid: int, cmid, date: int, text: str, recognized: bool):
    if not cmid:
        return
    db.execute(
        "INSERT INTO msg_log(peer_id,cmid,from_id,date,text,recognized,added_ts) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(peer_id,cmid) DO UPDATE SET text=excluded.text, recognized=excluded.recognized",
        (peer, cmid, uid, date, text, 1 if recognized or not worth_watching(text) else 0, int(time.time())),
    )
    db.commit()


def handle_chat(msg: dict):
    peer, uid, cmid = msg["peer_id"], msg["from_id"], msg.get("conversation_message_id")
    if msg.get("action"):
        return handle_action(peer, msg["action"])
    if uid <= 0:
        return
    register_chat(peer)
    text = MENTION_RE.sub("", msg.get("text") or "").strip()
    if not text or not is_command_like(text):
        return
    chat = get_chat(peer)
    if not chat or not chat["enabled"]:
        return  # беседа отключена в /настройка — бот её игнорирует
    ensure_admin(peer)
    effect = run_chat_command(peer, chat, uid, cmid, text, floor_min(msg["date"]), edited=False)
    log_message(peer, uid, cmid, msg["date"], text, effect)


def handle_chat_edit(msg: dict, source: str = "событие") -> bool:
    """
    Отредактированное сообщение в беседе (событие message_edit или запасной опрос). Если после правки
    в нём команда (/старт, /стоп, #лапа, #уход, «+ тренер»), она отрабатывается так, как если бы была
    отправлена в исходное время сообщения (правила — в process_chat_command). True — эффект был.
    """
    peer, uid, cmid = msg.get("peer_id", 0), msg.get("from_id", 0), msg.get("conversation_message_id")
    text = MENTION_RE.sub("", msg.get("text") or "").strip()
    tag = f"правка {source}: беседа {peer}, сообщение {cmid}, текст {text[:60]!r}"
    if msg.get("action") or uid <= 0 or not cmid:
        log.info("%s — пропущена (служебное сообщение или нет ID)", tag)
        return False
    date = msg.get("date") or int(time.time())
    row = db.execute("SELECT * FROM msg_log WHERE peer_id=? AND cmid=?", (peer, cmid)).fetchone()
    if row and row["text"] == text and row["recognized"]:
        log.info("%s — пропущена: этот текст уже обработан", tag)
        return False
    if not text or not is_command_like(text):
        log_message(peer, uid, cmid, date, text, True)  # перестало быть командой — следить больше не нужно
        log.info("%s — пропущена: в сообщении нет команды", tag)
        return False
    register_chat(peer)
    chat = get_chat(peer)
    if not chat or not chat["enabled"]:
        log.info("%s — пропущена: беседа отключена в /настройка", tag)
        return False
    effect = run_chat_command(peer, chat, uid, cmid, text, floor_min(date), edited=True)
    log_message(peer, uid, cmid, date, text, effect)
    log.info("%s — %s", tag, "команда отработана" if effect else "команды нет или она ничего не дала")
    return effect


def poll_watched_messages():
    """
    Запасной способ ловить правки, не зависящий от доставки события message_edit: раз в EDIT_POLL_SEC
    секунд перечитывает недавние сообщения, где команда не сработала, и, если текст изменился,
    обрабатывает их как правку.
    """
    now = int(time.time())
    db.execute("DELETE FROM msg_log WHERE added_ts<?", (now - MSG_LOG_KEEP_SEC,))
    rows = db.execute("SELECT * FROM msg_log WHERE recognized=0 AND added_ts>? ORDER BY peer_id, cmid",
                      (now - EDIT_WATCH_SEC,)).fetchall()
    by_peer = {}
    for r in rows:
        by_peer.setdefault(r["peer_id"], []).append(r)
    for peer, items_ in by_peer.items():
        for i in range(0, len(items_), 100):
            chunk = items_[i:i + 100]
            try:
                resp = vk.messages.getByConversationMessageId(
                    peer_id=peer, conversation_message_ids=",".join(str(w["cmid"]) for w in chunk))
            except ApiError as e:
                log.warning("Опрос правок: не удалось прочитать сообщения беседы %s: %s", peer, e)
                break
            found = {it.get("conversation_message_id"): it for it in resp.get("items", [])}
            for w in chunk:
                it = found.get(w["cmid"])
                if not it:
                    continue  # сообщение удалено
                new = MENTION_RE.sub("", it.get("text") or "").strip()
                if new == w["text"]:
                    continue
                db.execute("UPDATE msg_log SET text=? WHERE peer_id=? AND cmid=?", (new, peer, w["cmid"]))
                db.commit()
                handle_chat_edit(dict(it, peer_id=peer), source="опрос")
    db.commit()


# ───────────────────────── свободный бег ─────────────────────────
def open_run(peer: int):
    return db.execute("SELECT * FROM trainings WHERE peer_id=? AND kind='free_run' AND status='run' "
                      "ORDER BY id DESC LIMIT 1", (peer,)).fetchone()


def new_run(peer: int, start_ts: int, now: int):
    cur = db.execute("INSERT INTO trainings(peer_id,leader_id,start_ts,status,kind,touch_ts) "
                     "VALUES(?,?,?,'run','free_run',?)", (peer, 0, start_ts, now))
    db.commit()
    return db.execute("SELECT * FROM trainings WHERE id=?", (cur.lastrowid,)).fetchone()


def find_open_row(run, name: str, vk_id):
    for r in db.execute("SELECT * FROM participants WHERE training_id=? AND leave_ts IS NULL", (run["id"],)):
        if name_key(r["name"]) == name_key(name) or (vk_id and r["vk_id"] == vk_id):
            return r
    return None


def find_closed_row(run, name: str, vk_id):
    for r in db.execute("SELECT * FROM participants WHERE training_id=? AND leave_ts IS NOT NULL ORDER BY id DESC",
                        (run["id"],)):
        if name_key(r["name"]) == name_key(name) or (vk_id and r["vk_id"] == vk_id):
            return r
    return None


def add_flags(pid: int, flags, src: str = None):
    """Дописывает пометки (без повторов) и текст отписи к участнику."""
    r = db.execute("SELECT flag FROM participants WHERE id=?", (pid,)).fetchone()
    cur = [x for x in (r["flag"] or "").split("; ") if x]
    for f in flags:
        if f not in cur:
            cur.append(f)
    db.execute("UPDATE participants SET flag=?, src=COALESCE(?, src) WHERE id=?",
               ("; ".join(cur) or None, src if cur else None, pid))


def author_info(uid: int, members) -> dict:
    rec = members.trainer(uid) if members else None
    if rec and rec["name"]:
        return {"vk_id": uid, "name": rec["name"], "known": True}
    return {"vk_id": uid, "name": norm_name(user_name(uid)), "known": False}


def apply_free_run_person(peer: int, cmid, seg: str, pr: dict, P: dict, ts: int, now: int) -> bool:
    name, vk_id = P["name"], P.get("vk_id")
    flags = list(pr["flags"]) + list(P.get("flags", []))
    in_ts = pr["in_ts"] if pr["in_ts"] is not None else ts
    out_ts = pr["out_ts"] if pr["out_ts"] is not None else ts
    run = open_run(peer)
    row = find_open_row(run, name, vk_id) if run else None
    pid = row["id"] if row else None
    if pr["deduct"] and not (pr["has_in"] or pr["has_out"]):
        return apply_deduct_only(peer, cmid, seg, pr, P, flags, now)

    def insert(join_ts):
        nonlocal run
        run = run or new_run(peer, join_ts, now)
        if join_ts < run["start_ts"]:
            db.execute("UPDATE trainings SET start_ts=? WHERE id=?", (join_ts, run["id"]))
        cur = db.execute("INSERT INTO participants(training_id,num,vk_id,name,join_ts,join_cmid) VALUES(?,?,?,?,?,?)",
                         (run["id"], next_num(run["id"]), vk_id, name, join_ts, cmid))
        return cur.lastrowid

    if pr["has_in"]:
        if pid:
            flags.append("повторный приход: участник уже отмечен в забеге")
        else:
            pid = insert(in_ts)
    if pr["has_out"]:
        if pid is None:
            closed = find_closed_row(run, name, vk_id) if run and not pr["has_in"] else None
            if closed:  # уход уже был отписан — не создаём пустую запись, только помечаем
                add_flags(closed["id"], ["повторный уход: участник уже отмечен ушедшим"], seg[:200])
                db.commit()
                return True
            pid = insert(out_ts)
            flags.append("нет прихода — укажите время начала")
        r = db.execute("SELECT * FROM participants WHERE id=?", (pid,)).fetchone()
        if out_ts < r["join_ts"]:
            flags.append("время ухода раньше времени прихода")
        db.execute("UPDATE participants SET leave_ts=?, leave_cmid=?, auto_leave=0 WHERE id=?",
                   (max(out_ts, r["join_ts"]), cmid, pid))
    if pr["deduct"]:
        db.execute("UPDATE participants SET deduct=?, deduct_cmid=? WHERE id=?", (pr["deduct"], cmid, pid))
    add_flags(pid, flags, seg[:200])
    run_id = db.execute("SELECT training_id FROM participants WHERE id=?", (pid,)).fetchone()[0]
    db.execute("UPDATE trainings SET touch_ts=? WHERE id=?", (now, run_id))
    db.commit()
    return True


def apply_deduct_only(peer: int, cmid, seg: str, pr: dict, P: dict, flags, now: int) -> bool:
    """«вычтите мне 15 минут» без прихода/ухода: вычет у последней записи человека за 12 часов."""
    rows = db.execute(
        "SELECT p.*, t.status AS tstatus FROM participants p JOIN trainings t ON t.id=p.training_id "
        "WHERE t.peer_id=? AND t.kind='free_run' AND t.status IN ('run','finished') AND p.join_ts>? "
        "ORDER BY p.id DESC", (peer, now - 12 * 3600)).fetchall()
    for r in rows:
        if name_key(r["name"]) == name_key(P["name"]) or (P.get("vk_id") and r["vk_id"] == P["vk_id"]):
            db.execute("UPDATE participants SET deduct=?, deduct_cmid=? WHERE id=?", (pr["deduct"], cmid, r["id"]))
            add_flags(r["id"], flags, seg[:200])
            db.commit()
            if r["tstatus"] == "finished":
                update_all_reports(r["training_id"])
            return True
    return False


def free_run_history(peer: int, uid: int, cmid, ts: int) -> dict:
    """Недавние отписи с именами: у этого автора (для сообщений без имени) и в беседе (для выбора между похожими)."""
    last, sender, chat = None, [], []
    for r in db.execute("SELECT * FROM free_run_msgs WHERE peer_id=? AND ts>? AND ts<=? AND cmid<>? "
                        "ORDER BY ts DESC, cmid DESC LIMIT 80", (peer, ts - 24 * 3600, ts + 60, cmid or 0)):
        people = json.loads(r["names"])
        for p in people:
            if p["name"] not in chat:
                chat.append(p["name"])
        if r["from_id"] == uid:
            if last is None and ts - r["ts"] <= 12 * 3600:
                last = people
            for p in people:
                if p["name"] not in sender:
                    sender.append(p["name"])
    return {"last": last or [], "sender": sender, "chat": chat}


def apply_free_run(peer: int, cmid, seg: str, pr: dict, ts: int, now: int) -> bool:
    if pr.get("invalid") or not (pr["has_in"] or pr["has_out"] or pr["deduct"]):
        return False
    people = pr["people"]
    if pr["all"]:  # «все ушли» — все, кто сейчас в забеге
        run = open_run(peer)
        people = [{"name": r["name"], "vk_id": r["vk_id"], "flags": []} for r in db.execute(
            "SELECT * FROM participants WHERE training_id=? AND leave_ts IS NULL", (run["id"],))] if run else []
        pr = dict(pr, has_in=False)
    effect = False
    for P in people:
        effect = apply_free_run_person(peer, cmid, seg, pr, P, ts, now) or effect
    return effect


def free_run_undo(peer: int, cmid, allow_sent: bool = False, touched: list = None) -> bool:
    """
    Снимает действия сообщения cmid. По умолчанию — только в ещё не закрытом забеге (False — забег уже отправлен).
    allow_sent=True (сообщение удалено) — и в забеге, отчёт по которому отправлен, но ещё не подтверждён;
    touched — туда попадают id таких забегов, чтобы обновить их отчёты.
    """
    rows = db.execute(
        "SELECT p.*, t.status AS tstatus FROM participants p JOIN trainings t ON t.id=p.training_id "
        "WHERE t.peer_id=? AND t.kind='free_run' AND (p.join_cmid=? OR p.leave_cmid=? OR p.deduct_cmid=?)",
        (peer, cmid, cmid, cmid)).fetchall()
    ok = ("run", "finished") if allow_sent else ("run",)
    if any(r["tstatus"] not in ok for r in rows):
        return False
    for r in rows:
        sent = r["tstatus"] != "run"
        if sent and touched is not None:
            touched.append(r["training_id"])
        if r["join_cmid"] == cmid:
            db.execute("DELETE FROM participants WHERE id=?", (r["id"],))
            continue
        db.execute("UPDATE participants SET flag=NULL, src=NULL WHERE id=?", (r["id"],))
        if r["leave_cmid"] == cmid:
            if sent:  # забег уже закрыт: без сообщения об уходе участник остаётся «без ухода»
                db.execute("UPDATE participants SET leave_ts=join_ts, leave_cmid=NULL, auto_leave=1 WHERE id=?",
                           (r["id"],))
                add_flags(r["id"], ["нет ухода: сообщение с уходом удалено — укажите время ухода"])
            else:
                db.execute("UPDATE participants SET leave_ts=NULL, leave_cmid=NULL WHERE id=?", (r["id"],))
        if r["deduct_cmid"] == cmid:
            db.execute("UPDATE participants SET deduct=0, deduct_cmid=NULL WHERE id=?", (r["id"],))
    db.commit()
    return True


# ───────────── отладочные сообщения о непонятых отписях ─────────────
DEBUG_MODES = ("owner", "admins", "off")  # кому слать: владельцу / всем администраторам сообщества / никому
DEBUG_LABELS = {"owner": "владелец сообщества", "admins": "администраторы сообщества", "off": "выключены"}
DEBUG_SHORT = {"owner": "владелец", "admins": "администраторы", "off": "выкл"}
DEBUG_LIMIT, DEBUG_WINDOW = 20, 600  # не больше 20 отладочных сообщений за 10 минут (защита от спама в беседе)
_debug_times, _debug_seen = [], set()


def get_debug_mode() -> str:
    r = db.execute("SELECT value FROM bot_settings WHERE key='debug_audience'").fetchone()
    return r["value"] if r and r["value"] in DEBUG_MODES else "owner"


def debug_recipients() -> set:
    mode = get_debug_mode()
    if mode == "off":
        return set()
    return group_owner_ids() if mode == "owner" else group_admin_ids()


def send_debug(key, text: str):
    """Отправляет отладочное сообщение получателям из настройки. Одно и то же — не дважды; не чаще лимита."""
    uids = debug_recipients()
    if not uids:
        if get_debug_mode() != "off":
            log.warning("Отладочное сообщение некому отправить: не определён владелец сообщества "
                        "(задайте BOT_OWNER_IDS в .env или выдайте ключу доступ к списку руководителей)")
        return
    now = time.time()
    if key in _debug_seen:
        return
    _debug_times[:] = [x for x in _debug_times if now - x < DEBUG_WINDOW]
    if len(_debug_times) >= DEBUG_LIMIT:
        log.warning("Лимит отладочных сообщений (%s за %s с) — это пропущено", DEBUG_LIMIT, DEBUG_WINDOW)
        return
    if len(_debug_seen) > 2000:
        _debug_seen.clear()
    _debug_seen.add(key)
    _debug_times.append(now)
    for u in sorted(uids):
        send_dm(u, text[:3900])


def free_run_fail_reason(pr: dict) -> str:
    """Почему отпись не дала эффекта (для отладочного сообщения)."""
    if pr.get("invalid"):
        return "в тексте несуществующее время (часы больше 23 или минуты больше 59) — отпись отклонена целиком"
    if not (pr["has_in"] or pr["has_out"] or pr["deduct"]):
        why = ("не найдено ни действие (пришёл/ушёл и т. п.), ни время, которое можно связать с приходом "
               "или уходом, ни вычет минут")
        if pr["explicit"]:
            why += "; имя распознано, а что человек сделал — нет"
        return why
    if pr["all"]:
        return "«все ушли»: в открытом забеге нет ни одного участника без отметки ухода"
    if pr["deduct"] and not (pr["has_in"] or pr["has_out"]):
        names = ", ".join(p["name"] for p in pr["people"]) or "—"
        return f"вычет {pr['deduct']} мин: не найдена запись свободного бега за последние 12 часов у: {names}"
    return "действие распознано, но применить его не удалось (причина не определена — смотри лог бота)"


def report_misunderstood(peer: int, uid: int, cmid, text: str, ts: int, edited: bool, failed):
    """Дебаг-лог в ЛС: бот поставил ❓ на сообщение в беседе — что именно не получилось. Никогда не бросает исключений."""
    try:
        def act(has, t_):
            return "нет" if not has else (f"{dt(t_):%d.%m %H:%M}" if t_ is not None else "по времени сообщения")

        lines = [
            "🐞 Бот не понял сообщение в беседе (поставил реакцию ❓)", "",
            f"Беседа: {chat_title(peer)} (№{peer - 2_000_000_000})",
            f"Автор: {user_link(uid)} (id{uid})",
            f"Отправлено: {dt(ts):%d.%m.%Y %H:%M} МСК, сообщение №{cmid}" + (" — после правки" if edited else ""),
            f"Текст: «{text[:600]}»", "", "Что пошло не так:",
        ]
        for i, (seg, pr) in enumerate(failed, 1):
            people = ", ".join(p["name"] for p in pr["people"]) or "никого"
            flags = list(dict.fromkeys(list(pr["flags"]) + [f for p in pr["people"] for f in p.get("flags", [])]))
            lines += [
                f"{i}. Отпись «{seg[:250]}»",
                f"   Причина: {free_run_fail_reason(pr)}",
                f"   Разобрано: люди — {people} ({'названы в тексте' if pr['explicit'] else 'в тексте не названы'}); "
                f"приход — {act(pr['has_in'], pr['in_ts'])}; уход — {act(pr['has_out'], pr['out_ts'])}; "
                f"«все» — {'да' if pr['all'] else 'нет'}; вычет — {pr['deduct']} мин",
            ]
            if flags:
                lines.append("   Пометки разбора: " + "; ".join(flags))
            if pr.get("unknown"):
                lines.append("   Нераспознанные слова: " + ", ".join(pr["unknown"][:15]))
            lines.append(f"   Текст в нормализованном виде: «{(pr.get('norm') or '')[:250]}»")
        lines += ["", "Если автор поправит сообщение, бот перечитает его; иначе добавь участника вручную в отчёте."]
        send_debug((peer, cmid, hash(text)), "\n".join(lines))
    except Exception:  # noqa: BLE001
        log.exception("Не удалось отправить отладочное сообщение")


def process_free_run(peer: int, chat, uid: int, cmid, text: str, ts: int, edited: bool = False) -> bool:
    """#бег в беседе со свободным бегом. True — понято (или осознанно пропущено); False — не поняли (❓)."""
    segs = freerun.segments(text)
    if not segs:
        return False
    members = get_members()
    if edited and not FREE_RUN_SHADOW and not free_run_undo(peer, cmid):
        return True  # забег уже отправлен администраторам — правки только кнопками отчёта
    author = author_info(uid, members)
    history = free_run_history(peer, uid, cmid, ts)
    now, effect, named, failed = int(time.time()), False, [], []
    for seg in segs:
        pr = freerun.parse(seg, ts, members, author, history)
        if pr["explicit"] and not pr["invalid"]:
            named += [{"name": p["name"], "vk_id": p.get("vk_id")} for p in pr["people"]]
        log.info("Свободный бег, беседа %s, сообщение %s: %r → люди=%s приход=%s уход=%s все=%s вычет=%s пометки=%s",
                 peer, cmid, seg[:80], [p["name"] for p in pr["people"]], pr["has_in"], pr["has_out"],
                 pr["all"], pr["deduct"], pr["flags"])
        if not FREE_RUN_SHADOW:
            ok = apply_free_run(peer, cmid, seg, pr, ts, now)
            effect = ok or effect
            if not ok:
                failed.append((seg, pr))
    if named and cmid:  # запоминаем, о ком писал автор: пригодится для сообщений без имени и похожих имён
        db.execute("INSERT OR REPLACE INTO free_run_msgs(peer_id,cmid,from_id,ts,names) VALUES(?,?,?,?,?)",
                   (peer, cmid, uid, ts, json.dumps(named, ensure_ascii=False)))
        db.commit()
    if FREE_RUN_SHADOW:
        return True
    react(peer, cmid, "check" if effect else "question")
    if not effect:
        report_misunderstood(peer, uid, cmid, text, ts, edited, failed)
    return effect


def finalize_free_run(t):
    tid = t["id"]
    r = db.execute("SELECT MIN(join_ts) a, MAX(COALESCE(leave_ts, join_ts)) b, COUNT(*) n FROM participants "
                   "WHERE training_id=?", (tid,)).fetchone()
    if not r["n"]:
        db.execute("DELETE FROM trainings WHERE id=?", (tid,))
        db.commit()
        return
    db.execute("UPDATE trainings SET status='finished', start_ts=?, end_ts=? WHERE id=?", (r["a"], r["b"], tid))
    db.commit()
    chat_title(t["peer_id"], refresh=True)
    autocorrect_participants(tid)
    dispatch_report(tid)


def free_run_tick():
    """Раз в 15 секунд: закрывает «забытые» уходы (8 ч) и отправляет отчёт по забегам, где все ушли и тихо."""
    now = int(time.time())
    for t in db.execute("SELECT * FROM trainings WHERE kind='free_run' AND status='run'").fetchall():
        for p in db.execute("SELECT * FROM participants WHERE training_id=? AND leave_ts IS NULL AND join_ts<?",
                            (t["id"], now - FREE_RUN_MAX_SEC)).fetchall():
            db.execute("UPDATE participants SET leave_ts=join_ts, auto_leave=1 WHERE id=?", (p["id"],))
            add_flags(p["id"], [f"нет ухода за {FREE_RUN_MAX_SEC // 3600} ч — укажите время ухода"])
            db.execute("UPDATE trainings SET touch_ts=? WHERE id=?", (now, t["id"]))
        db.commit()
        left = db.execute("SELECT COUNT(*) FROM participants WHERE training_id=? AND leave_ts IS NULL",
                          (t["id"],)).fetchone()[0]
        if left == 0 and now - (t["touch_ts"] or now) >= get_free_run_delay() * 60:
            finalize_free_run(t)


def run_chat_command(peer: int, chat, uid: int, cmid, text: str, ts: int, edited: bool) -> bool:
    if chat["free_run"] and freerun.RE_RUN.search(text):
        return process_free_run(peer, chat, uid, cmid, text, ts, edited)
    return process_chat_command(peer, chat, uid, cmid, text, ts, edited=edited)


# ───────────────────────── правки в ЛС ─────────────────────────
class EditError(Exception):
    pass


TIME = r"(\d{1,2})[:.](\d{2})"
RE_RENAME = re.compile(r"^\[?(\d+)\]?\s+имя\s+(.+)$", re.I)
RE_FIX = re.compile(rf"^\[?(\d+)\]?\s+{TIME}\s*[-–—]\s*{TIME}$")
RE_PM = re.compile(rf"^([+\-])\s*(.+?)(?:\s+{TIME}\s*[-–—]\s*{TIME})?$")
# строки про тренеров: «тренер 1 15:00-16:30», «+ тренер Имя 15:00-16:30», «- тренер 2»
RE_T_FIX = re.compile(rf"^тренер\s+\[?(\d+)\]?\s+{TIME}\s*[-–—]\s*{TIME}$", re.I)
RE_T_PM = re.compile(rf"^([+\-])\s*тренер\s+(.+?)(?:\s+{TIME}\s*[-–—]\s*{TIME})?$", re.I)
RE_T_RENAME = re.compile(r"^тренер\s+\[?(\d+)\]?\s+имя\s+(.+)$", re.I)
RE_VK_REF = re.compile(r"^(?:https?://)?(?:m\.)?(?:vk\.(?:com|ru)/)?id(\d+)/?$", re.I)


def _candidates(base_ts: int, h, m):
    """Моменты времени ЧЧ:ММ на сутки до старта тренировки и на 2 суток после (по возрастанию)."""
    h, m = int(h), int(m)
    if h > 23 or m > 59:
        raise EditError("bad time")
    d0 = dt(base_ts).replace(hour=h, minute=m, second=0, microsecond=0)
    return [int((d0 + timedelta(days=k)).timestamp()) for k in (-1, 0, 1, 2)]


def _span(t, h1, m1, h2, m2):
    """
    Переводит «ЧЧ:ММ-ЧЧ:ММ» в моменты времени с учётом фактического окна тренировки
    (в том числе перехода через полночь: 23:00 → 01:30 следующего дня).
    Приход — первое такое время не раньше (старт − 6 ч); уход — первое такое время после прихода.
    """
    start, end = t["start_ts"], t["end_ts"]
    fr = t["kind"] == "free_run"  # у свободного бега у каждого своё время: допуск шире
    tol = 12 * 3600 if fr else JOIN_TOLERANCE_SEC
    j = next((c for c in _candidates(start, h1, m1) if c >= start - tol), None)
    l = next((c for c in _candidates(start, h2, m2) if j is not None and c > j), None)
    if j is None or l is None or j > end + (tol if fr else 0) or l > end + tol:
        raise EditError("время вне тренировки")
    return j, l


def resolve_trainer_ref(arg: str, members):
    """«id123», ссылка на страницу или имя на варе → (VK ID | None, имя | None)."""
    m = RE_VK_REF.match(arg.strip())
    if m:
        return int(m.group(1)), None
    name = norm_name(arg)
    if not name:
        raise EditError("пустое имя тренера")
    rec = members.participant(name) if members else None
    if rec:
        return rec["vk_id"], rec["name"] or name
    return None, name


def apply_edits(tid: int, lines):
    t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    ensure_trainers(t)
    members = get_members()  # до транзакции: может обращаться к Google Таблице
    with db:  # атомарно: при ошибке откат всех строк
        for ln in lines:
            if t["kind"] == "free_run" and re.match(r"^[+\-]?\s*тренер", ln, re.I):
                raise EditError("в свободном беге нет тренеров")
            m = RE_T_RENAME.match(ln)
            if m:
                name = norm_name(m.group(2))
                if not name:
                    raise EditError("пустое имя")
                cur = db.execute("UPDATE trainers SET name=? WHERE training_id=? AND num=?",
                                 (name, tid, int(m.group(1))))
                if cur.rowcount == 0:
                    raise EditError(f"нет тренера {m.group(1)}")
                continue
            m = RE_T_FIX.match(ln)
            if m:
                j, l = _span(t, *m.groups()[1:])
                cur = db.execute("UPDATE trainers SET join_ts=?, leave_ts=? WHERE training_id=? AND num=?",
                                 (j, l, tid, int(m.group(1))))
                if cur.rowcount == 0:
                    raise EditError(f"нет тренера {m.group(1)}")
                continue
            m = RE_T_PM.match(ln)
            if m:
                sign, arg, times = m.group(1), m.group(2).strip(), m.groups()[2:]
                if sign == "+":
                    vk_id, name = resolve_trainer_ref(arg, members)
                    j, l = _span(t, *times) if all(times) else (t["start_ts"], t["end_ts"])
                    db.execute(
                        "INSERT INTO trainers(training_id,num,vk_id,name,join_ts,leave_ts) VALUES(?,?,?,?,?,?)",
                        (tid, next_trainer_num(tid), vk_id, name, j, l),
                    )
                else:
                    if arg.isdigit():
                        removed = db.execute("DELETE FROM trainers WHERE training_id=? AND num=?",
                                             (tid, int(arg))).rowcount
                    else:
                        key = name_key(arg)
                        ids = [r["id"] for r in db.execute("SELECT * FROM trainers WHERE training_id=?", (tid,))
                               if name_key(trainer_view(r, members)[0]) == key]
                        for i in ids:
                            db.execute("DELETE FROM trainers WHERE id=?", (i,))
                        removed = len(ids)
                    if removed == 0:
                        raise EditError(f"не найден тренер: {arg}")
                continue
            if re.match(r"^[+\-]\s*тренер\s*$", ln, re.I):
                raise EditError("после «тренер» нужно указать имя или номер")
            m = RE_RENAME.match(ln)
            if m:
                name = norm_name(m.group(2))
                if not name:
                    raise EditError("пустое имя")
                cur = db.execute(
                    "UPDATE participants SET name=? WHERE training_id=? AND num=?",
                    (name, tid, int(m.group(1))),
                )
                if cur.rowcount == 0:
                    raise EditError(f"нет участника {m.group(1)}")
                continue
            m = RE_FIX.match(ln)
            if m:
                num = int(m.group(1))
                j, l = _span(t, *m.groups()[1:])
                cur = db.execute(
                    "UPDATE participants SET join_ts=?, leave_ts=?, auto_leave=0 "
                    "WHERE training_id=? AND num=?",
                    (j, l, tid, num),
                )
                if cur.rowcount == 0:
                    raise EditError(f"нет участника {num}")
                continue
            m = RE_PM.match(ln)
            if not m:
                raise EditError(f"не понял строку: {ln}")
            sign, arg = m.group(1), m.group(2).strip()
            times = m.groups()[2:]
            if sign == "+":
                name = norm_name(arg)
                if not name:
                    raise EditError("пустое имя")
                if all(times):
                    j, l = _span(t, *times)
                else:
                    j, l = t["start_ts"], t["end_ts"]
                db.execute(
                    "INSERT INTO participants(training_id,num,vk_id,name,join_ts,leave_ts) "
                    "VALUES(?,?,NULL,?,?,?)",
                    (tid, next_num(tid), name, j, l),
                )
            else:
                if arg.isdigit():
                    cur = db.execute(
                        "DELETE FROM participants WHERE training_id=? AND num=?", (tid, int(arg))
                    )
                    removed = cur.rowcount
                else:
                    key = name_key(arg)
                    ids = [r["id"] for r in db.execute(
                        "SELECT id,name FROM participants WHERE training_id=?", (tid,)
                    ) if name_key(r["name"]) == key]
                    for i in ids:
                        db.execute("DELETE FROM participants WHERE id=?", (i,))
                    removed = len(ids)
                if removed == 0:
                    raise EditError(f"не найден: {arg}")


RE_S_NAME = re.compile(r"^имя\s+(.+)$", re.I | re.S)
RE_S_TIME = re.compile(r"^время\s+(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})$", re.I)


def get_session(uid: int):
    return db.execute("SELECT * FROM edit_sessions WHERE user_id=?", (uid,)).fetchone()


def clear_session(uid: int, cmid=None):
    """Удаляет сессию; если указан cmid — только если она связана с этим сообщением."""
    if cmid is None:
        db.execute("DELETE FROM edit_sessions WHERE user_id=?", (uid,))
    else:
        db.execute("DELETE FROM edit_sessions WHERE user_id=? AND cmid=?", (uid, cmid))
    db.commit()


def entry_mark(t, p, members) -> str:
    """
    Свободный бег: непроверенные пометки бота у участника. «🚨» — нужна ручная правка (или персонаж не найден
    на листе «Участники»), «ℹ️» — только автоматические правки, «» — всё чисто / участник подтверждён.
    """
    if t["kind"] != "free_run":
        return ""
    unmatched = bool(members) and not p["verified"] and not members.participant(p["name"], p["vk_id"])
    if unmatched or (p["flag"] and render_flags(p["flag"])[1]):
        return "🚨"
    return "ℹ️" if p["flag"] else ""


def get_entry(tid: int, k: str, n: int):
    tbl = "trainers" if k == "t" else "participants"
    return db.execute(f"SELECT * FROM {tbl} WHERE training_id=? AND num=?", (tid, n)).fetchone()


def entry_text(tid: int, k: str, n: int):
    row = get_entry(tid, k, n)
    if not row:
        return None
    t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    if k == "t":
        name = trainer_view(row, get_members())[0]
        leave = row["leave_ts"] or t["end_ts"]
    else:
        name, leave = row["name"], row["leave_ts"] or t["end_ts"]
    marks = ""
    if k == "p" and t["kind"] == "free_run":
        members = get_members()
        lines = render_flags(row["flag"])[0] if row["flag"] else []
        if members and not row["verified"] and not members.participant(row["name"], row["vk_id"]):
            lines.append("🚨 Персонаж не найден на листе «Участники» — CW ID не будет записан")
        if lines:
            marks = ("\n\nПометки бота:\n" + "\n".join(lines) +
                     "\n«Подтвердить правки» снимает пометки: участник считается проверенным.")
    return (f"Выбран {'тренер' if k == 't' else 'участник'}: {n}. {name}, "
            f"{fmt_t(row['join_ts'], t['start_ts'])} - {fmt_t(leave, t['start_ts'])}{marks}\n\n"
            "Отправь ответным сообщением:\n"
            "имя Новое Имя — сменить имя\n"
            "время 15:00-16:30 — изменить время участия\n\n"
            "«Удалить» убирает из отчёта, «Вернуться» закрывает это сообщение.")


def entry_kb(tid: int, k: str, n: int) -> str:
    kb = VkKeyboard(inline=True)
    row = get_entry(tid, k, n) if k == "p" else None
    if row is not None:
        t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
        if entry_mark(t, row, get_members()):  # есть непроверенные пометки бота
            kb.add_callback_button("Подтвердить правки", VkKeyboardColor.POSITIVE,
                                   payload={"cmd": "rp_ok", "tid": tid, "k": k, "n": n})
            kb.add_line()
    kb.add_callback_button("Удалить", VkKeyboardColor.NEGATIVE, payload={"cmd": "rp_del", "tid": tid, "k": k, "n": n})
    kb.add_callback_button("Вернуться", VkKeyboardColor.SECONDARY, payload={"cmd": "rp_back"})
    return kb.get_keyboard()


ADD_TEXT_FR = (
    "Добавление включено: я слушаю новые сообщения. Отправь ответным сообщением (можно несколько строк):\n\n"
    "Имя 15:00-16:30 — добавить участника\n\n"
    "Время можно не указывать — тогда человек будет записан на весь забег. "
    "Чтобы закончить, нажми «Вернуться»."
)
ADD_TEXT = (
    "Добавление включено: я слушаю новые сообщения. Отправь ответным сообщением (можно несколько строк):\n\n"
    "Имя 15:00-16:30 — добавить участника\n"
    "тренер Имя 15:00-16:30 — добавить тренера (имя на варе или id123)\n\n"
    "Время можно не указывать — тогда человек будет записан на всю тренировку. "
    "Чтобы закончить, нажми «Вернуться»."
)


def open_session(uid: int, tid: int, kind: str, num, text: str, kb: str):
    old = get_session(uid)
    if old and old["cmid"]:
        delete_msg(uid, old["cmid"])  # одновременно активно только одно сообщение правок
    cmid = send_with_kb(uid, text, kb)
    if cmid is None:
        clear_session(uid)
        return
    db.execute("INSERT OR REPLACE INTO edit_sessions(user_id,training_id,kind,num,cmid,ts) VALUES(?,?,?,?,?,?)",
               (uid, tid, kind, num, cmid, int(time.time())))
    db.commit()


def apply_session_lines(sess, lines):
    tid, kind, n = sess["training_id"], sess["kind"], sess["num"]
    conv = []
    for ln in lines:
        if kind == "add":
            if ln[:1] in ("+", "-"):
                conv.append(ln)
                continue
            if not re.search(r"[^\W\d_]", re.sub(r"\d{1,2}[:.]\d{2}", "", ln)):
                raise EditError("нет имени")
            conv.append("+ " + ln)
            continue
        pre = f"тренер {n}" if kind == "t" else f"{n}"
        m = RE_S_NAME.match(ln)
        if m:
            conv.append(f"{pre} имя {m.group(1)}")
            continue
        m = RE_S_TIME.match(ln)
        if not m:
            raise EditError(f"не понял строку: {ln}")
        conv.append(f"{pre} {m.group(1)}:{m.group(2)}-{m.group(3)}:{m.group(4)}")
    apply_edits(tid, conv)


RE_DATE = r"(?:(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+)?"  # необязательная дата «24.10» / «24.10.2026»
RE_TIMERANGE = re.compile(rf"^(?:время\s+)?{RE_DATE}{TIME}\s*[-–—]\s*{TIME}$", re.I)
RE_INDIV = re.compile(rf"^{RE_DATE}{TIME}\s*[-–—]\s*{TIME}\s+(.+)$", re.S)
RE_MENTION_ID = re.compile(r"^\[id(\d+)\|[^\]]*\]$", re.I)

TIME_TEXT = (
    "Отправь новое время тренировки ответным сообщением:\n\n"
    "15:00-16:30\n"
    "24.10.2026 15:00-16:30 — если нужно изменить и дату\n\n"
    "Те, кто был на тренировке с самого начала или до конца, сдвинутся вместе с ней; остальные "
    "подрежутся по новым границам. Чтобы отменить, нажми «Вернуться»."
)
IND_TEXT = (
    "Индивидуальная тренировка. Отправь одним сообщением дату, время и тренирующегося:\n\n"
    "24.10.2026 15:00-16:30 Лихушек\n"
    "24.10 15:00-16:30 id123456\n\n"
    "Тренирующийся — имя на варе, ID ВК или ссылка на страницу. Дату можно не указывать — тогда возьмётся "
    "сегодняшняя. Тренером запишешься ты (в отчёте это можно поменять). Чтобы отменить, нажми «Вернуться»."
)


def build_range(dd, mo, yy, h1, m1, h2, m2, base_ts: int, prefer_past: bool = False):
    """
    «[дата] ЧЧ:ММ-ЧЧ:ММ» → (начало, конец) в unix-времени. Без даты берутся сутки base_ts
    (prefer_past — если время получилось в будущем больше чем на час, берутся вчерашние сутки).
    Конец раньше начала — это переход через полночь.
    """
    h1, m1, h2, m2 = int(h1), int(m1), int(h2), int(m2)
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise EditError("некорректное время")
    if (h1, m1) == (h2, m2):
        raise EditError("начало и конец совпадают")
    if dd:
        year = int(yy) if yy else dt(base_ts).year
        year += 2000 if year < 100 else 0
        try:
            d0 = datetime(year, int(mo), int(dd), h1, m1, tzinfo=MSK)
        except ValueError:
            raise EditError("некорректная дата")
    else:
        d0 = dt(base_ts).replace(hour=h1, minute=m1, second=0, microsecond=0)
        if prefer_past and int(d0.timestamp()) > base_ts + 3600:
            d0 -= timedelta(days=1)
    d1 = d0.replace(hour=h2, minute=m2)
    if d1 <= d0:
        d1 += timedelta(days=1)
    return int(d0.timestamp()), int(d1.timestamp())


def set_training_time(tid: int, ns: int, ne: int):
    """
    Меняет общее время тренировки. Те, кто пришёл ровно в старт / ушёл ровно в конец (в т.ч. «ушли по /стоп»),
    переезжают на новые границы; остальных подрезаем, чтобы они не выходили за новую тренировку.
    """
    t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    os_, oe = t["start_ts"], t["end_ts"]
    with db:
        for tbl in ("participants", "trainers"):
            q = f"UPDATE {tbl} SET "
            db.execute(q + "join_ts=? WHERE training_id=? AND join_ts=?", (ns, tid, os_))
            db.execute(q + "leave_ts=? WHERE training_id=? AND leave_ts=?", (ne, tid, oe))
            db.execute(q + "join_ts=? WHERE training_id=? AND join_ts<?", (ns, tid, ns))
            db.execute(q + "join_ts=? WHERE training_id=? AND join_ts>?", (ne, tid, ne))
            db.execute(q + "leave_ts=? WHERE training_id=? AND leave_ts>?", (ne, tid, ne))
            db.execute(q + "leave_ts=join_ts WHERE training_id=? AND leave_ts<join_ts", (tid,))
        db.execute("UPDATE trainings SET start_ts=?, end_ts=? WHERE id=?", (ns, ne, tid))


def resolve_trainee(arg: str):
    """Имя на варе, ID ВК, «id123», ссылка или [id123|Имя] → (VK ID | None, имя)."""
    arg = arg.strip()
    members = get_members()
    m = RE_VK_REF.match(arg) or RE_MENTION_ID.match(arg) or re.fullmatch(r"(\d+)", arg)
    if m:
        vk_id = int(m.group(1))
        rec = members.trainer(vk_id) if members else None
        return vk_id, (rec["name"] if rec and rec["name"] else user_name(vk_id))
    name = norm_name(arg)
    if not name:
        raise EditError("пустое имя")
    rec = members.participant(name) if members else None
    if rec:
        return rec["vk_id"], rec["name"] or name
    return None, name


def create_individual(uid: int, start_ts: int, end_ts: int, vk_id, name: str) -> int:
    """Индивидуальная тренировка: сразу завершена; тренер — модератор, тренирующийся — на всю тренировку."""
    cur = db.execute(
        "INSERT INTO trainings(peer_id,leader_id,stopper_id,start_ts,end_ts,status,kind) "
        "VALUES(?,?,?,?,?,'finished','individual')", (uid, uid, uid, start_ts, end_ts))
    tid = cur.lastrowid
    db.execute("INSERT INTO trainers(training_id,num,vk_id,join_ts,leave_ts) VALUES(?,?,?,?,?)",
               (tid, 1, uid, start_ts, end_ts))
    db.execute("INSERT INTO participants(training_id,num,vk_id,name,join_ts,leave_ts,auto_leave) "
               "VALUES(?,?,?,?,?,?,1)", (tid, 1, vk_id, name, start_ts, end_ts))
    db.commit()
    return tid


def start_individual(uid: int):
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("Вернуться", VkKeyboardColor.SECONDARY, payload={"cmd": "rp_back"})
    open_session(uid, 0, "indiv", None, IND_TEXT, kb.get_keyboard())


def handle_indiv_msg(uid: int, cmid, sess, lines) -> bool:
    if not is_group_moderator(uid):
        clear_session(uid)
        return False
    m = RE_INDIV.match(lines[0]) if len(lines) == 1 else None
    if not m:
        react(uid, cmid, "question")
        return True
    try:
        start, end = build_range(*m.groups()[:7], base_ts=int(time.time()), prefer_past=True)
        vk_id, name = resolve_trainee(m.group(8))
    except EditError as e:
        log.info("Индивидуальная тренировка отклонена: %s", e)
        react(uid, cmid, "question")
        return True
    tid = create_individual(uid, start, end, vk_id, name)
    autocorrect_participants(tid)
    react(uid, cmid, "check")
    delete_msg(uid, sess["cmid"])
    clear_session(uid, sess["cmid"])
    send_report_message(tid, uid)
    return True


def handle_time_msg(uid: int, cmid, sess, t, lines) -> bool:
    m = RE_TIMERANGE.match(lines[0]) if len(lines) == 1 else None
    if not m:
        react(uid, cmid, "question")
        return True
    try:
        ns, ne = build_range(*m.groups(), base_ts=t["start_ts"])
        set_training_time(t["id"], ns, ne)
    except EditError as e:
        log.info("Правка времени отклонена: %s", e)
        react(uid, cmid, "question")
        return True
    update_all_reports(t["id"])
    react(uid, cmid, "check")
    delete_msg(uid, sess["cmid"])
    clear_session(uid, sess["cmid"])
    return True


def handle_session_msg(uid: int, cmid, lines) -> bool:
    """Сообщение пользователя при открытом сообщении правок. True — обработано, False — разбирать по-старому."""
    sess = get_session(uid)
    if not sess:
        return False
    if time.time() - sess["ts"] > EDIT_LIMIT_SEC:
        clear_session(uid)
        return False
    if sess["kind"] == "indiv":
        return handle_indiv_msg(uid, cmid, sess, lines)
    t = db.execute("SELECT * FROM trainings WHERE id=?", (sess["training_id"],)).fetchone()
    if not t or t["status"] in ("confirmed", "rejected"):
        clear_session(uid)
        return False
    if sess["kind"] == "time":
        return handle_time_msg(uid, cmid, sess, t, lines)
    if sess["kind"] != "add" and not all(RE_S_NAME.match(l) or RE_S_TIME.match(l) for l in lines):
        return False
    tid = sess["training_id"]
    try:
        apply_session_lines(sess, lines)
    except EditError as e:
        log.info("Правка отклонена: %s", e)
        react(uid, cmid, "question")
        return True
    autocorrect_participants(tid)
    update_all_reports(tid)
    if sess["kind"] != "add":
        text = entry_text(tid, sess["kind"], sess["num"])
        if text:
            edit_msg(uid, sess["cmid"], text, entry_kb(tid, sess["kind"], sess["num"]))
    react(uid, cmid, "check")
    return True


# ───────────────────────── меню (постоянные кнопки над полем ввода) ─────────────────────────
MENU_SETTINGS, MENU_INDIV = "Настройка", "Индивидуальная тренировка"
MENU_PENDING = "Отчёты в обработке"
MENU_VERSION = 2  # при смене кнопок администраторам один раз показывается обновлённое меню


def menu_role(uid: int) -> str:
    return "admin" if is_group_admin(uid) else ("mod" if is_group_moderator(uid) else "")


def menu_kb(role: str) -> str:
    kb = VkKeyboard(one_time=False)
    if role == "admin":
        kb.add_button(MENU_SETTINGS, VkKeyboardColor.SECONDARY)
        kb.add_button(MENU_PENDING, VkKeyboardColor.SECONDARY)
        kb.add_line()
    kb.add_button(MENU_INDIV, VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def ensure_menu(uid: int, force: bool = False):
    """Показывает пользователю меню по его роли (один раз; снова — если роль изменилась). Ошибки не пробрасывает."""
    if uid <= 0:
        return
    try:
        role = menu_role(uid)
        key = f"admin:{MENU_VERSION}" if role == "admin" else role
        row = db.execute("SELECT role FROM menu_state WHERE user_id=?", (uid,)).fetchone()
        old = row["role"] if row else ""
        if key == old and not (force and role):
            return
        if role:
            names = (f"«{MENU_SETTINGS}», «{MENU_PENDING}» и «{MENU_INDIV}»" if role == "admin"
                     else f"«{MENU_INDIV}»")
            text, kb = f"Меню включено: {names} всегда под рукой над полем ввода.", menu_kb(role)
        else:
            text, kb = "Меню скрыто: у тебя больше нет доступа к его функциям.", json.dumps(
                {"buttons": [], "one_time": True})
        vk.messages.send(peer_id=uid, random_id=random.getrandbits(31), message=text,
                         keyboard=kb, disable_mentions=1)
        db.execute("INSERT OR REPLACE INTO menu_state(user_id,role,ts) VALUES(?,?,?)",
                   (uid, key, int(time.time())))
        db.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось показать меню %s: %s", uid, e)


def handle_menu(msg: dict) -> bool:
    """Нажатия кнопок меню (они приходят обычными сообщениями) и команда /меню. True — обработано."""
    uid = msg["from_id"]
    text = (msg.get("text") or "").strip().casefold()
    if text in ("/меню", "меню"):
        if menu_role(uid):
            ensure_menu(uid, force=True)
            return True
    elif text == MENU_SETTINGS.casefold():
        if is_group_admin(uid):
            show_settings(uid, force_new=True)
            return True
    elif text == MENU_PENDING.casefold():
        if is_group_admin(uid):
            show_pending(uid)
            return True
    elif text == MENU_INDIV.casefold():
        if is_group_moderator(uid):
            start_individual(uid)
            return True
    return False


def find_report(uid: int, msg: dict):
    reply = msg.get("reply_message")
    if reply and reply.get("conversation_message_id"):
        r = db.execute(
            "SELECT * FROM reports WHERE user_id=? AND cmid=?", (uid, reply["conversation_message_id"])
        ).fetchone()
        if r:
            tid = r["training_id"]
            return db.execute(
                "SELECT * FROM reports WHERE training_id=? AND user_id=? ORDER BY id DESC LIMIT 1", (tid, uid)
            ).fetchone()
    return db.execute(
        "SELECT r.* FROM reports r JOIN trainings t ON t.id=r.training_id "
        "WHERE r.user_id=? AND t.status NOT IN ('confirmed','rejected') ORDER BY r.id DESC LIMIT 1",
        (uid,),
    ).fetchone()


# ───────────────────────── /настройка (ЛС сообщества) ─────────────────────────
CMD_SETTINGS_RE = re.compile(r"^/настройка(?!\w)", re.I)
SETTINGS_RE = re.compile(r"^(\d+)\s+(вкл|выкл|сб\+|сб-|админы\+|админы-)$", re.I)
SELFGOV_RE = re.compile(r"^(\d+)\s+сам(?:оуправление)?\s*(.*)$", re.I)
SETTINGS_COLS = {
    "вкл": ("enabled", 1), "выкл": ("enabled", 0),
    "сб+": ("free_run", 1), "сб-": ("free_run", 0),
    "админы+": ("admins_only", 1), "админы-": ("admins_only", 0),
}
SETTINGS_HELP = "Нажми на беседу, чтобы изменить её настройки."

DAY_ALIASES = {
    "пн": 0, "пон": 0, "понедельник": 0, "вт": 1, "вторник": 1, "ср": 2, "среда": 2, "чт": 3,
    "четверг": 3, "пт": 4, "пятница": 4, "сб": 5, "суббота": 5, "вс": 6, "воскресенье": 6,
}
DAY_GROUPS = {
    "будни": range(0, 5), "будние": range(0, 5), "выходные": range(5, 7),
    "ежедневно": range(7), "все": range(7), "каждый": range(7),
}
DAY_OFF = {"", "-", "выкл", "нет", "откл", "отключить", "отключен", "отключено", "0"}


def parse_days(arg: str) -> str:
    """«пн,ср», «пн-ср», «будни», «сб вс» → «0,2» (пн=0 … вс=6). Пустая строка — режим отключён."""
    arg = arg.strip().lower()
    if arg in DAY_OFF:
        return ""
    days = set()
    for tok in re.split(r"[,;\s]+", arg):
        if not tok:
            continue
        if tok in DAY_GROUPS:
            days |= set(DAY_GROUPS[tok])
        elif tok in DAY_ALIASES:
            days.add(DAY_ALIASES[tok])
        elif re.fullmatch(r"[а-яё]+-[а-яё]+", tok) and all(x in DAY_ALIASES for x in tok.split("-")):
            a, b = (DAY_ALIASES[x] for x in tok.split("-"))
            days |= {(a + k) % 7 for k in range((b - a) % 7 + 1)}  # «сб-пн» = сб, вс, пн
        else:
            raise EditError(f"не понял день недели: {tok}")
    return ",".join(str(d) for d in sorted(days))


def fmt_days(days: str) -> str:
    ds = sorted(int(x) for x in (days or "").split(",") if x.strip().isdigit())
    if not ds:
        return "отключен"
    return "ежедневно" if len(ds) == 7 else ", ".join(DAY_NAMES[d] for d in ds)


def send_dm(uid: int, text: str):
    try:
        vk.messages.send(peer_id=uid, random_id=random.getrandbits(31),
                         message=text, disable_mentions=1)
    except ApiError as e:
        log.warning("Не удалось отправить сообщение %s: %s", uid, e)


def refresh_titles():
    """Обновляет названия известных бесед одним запросом (пакетами по 100)."""
    peers = [r["peer_id"] for r in db.execute("SELECT peer_id FROM chats")]
    for i in range(0, len(peers), 100):
        try:
            r = vk.messages.getConversationsById(peer_ids=",".join(map(str, peers[i:i + 100])))
        except ApiError as e:
            log.warning("Не удалось обновить названия бесед: %s", e)
            return
        for it in r.get("items", []):
            title = (it.get("chat_settings") or {}).get("title")
            if title:
                _titles[it["peer"]["id"]] = title
                db.execute("UPDATE chats SET title=? WHERE peer_id=?", (title, it["peer"]["id"]))
    db.commit()


def render_settings() -> str:
    refresh_titles()
    chats = db.execute("SELECT * FROM chats ORDER BY peer_id").fetchall()
    blocks = []
    for c in chats:
        cid, title = c["peer_id"] - 2_000_000_000, c["title"] or "Без названия"
        if c["enabled"]:
            blocks.append("\n".join([
                f"{cid}. {title} — включён.",
                f"Свободный бег: {'включён' if c['free_run'] else 'отключен'}",
                f"Тренировки проводят: {'только администраторы' if c['admins_only'] else 'все участники'}",
                f"День самоуправления: {fmt_days(c['self_gov_days'])}",
            ]))
        else:
            blocks.append(f"{cid}. {title} — отключен.")
    if not chats:
        blocks.append("— пока нет бесед (бот увидит беседу после первого сообщения в ней)")
    # беседы разделены пустой строкой, параметры одной беседы — на соседних строках
    return ("Активные беседы:\n\n" + "\n\n".join(blocks) + "\n\n" + SETTINGS_HELP
            + f"\n\nОтчёт о свободном беге уходит администраторам: {fmt_delay(get_free_run_delay())} "
              "после последней отписи (временная настройка)."
            f"\n\nОтладочные сообщения о непонятых отписях (❓) получают: {DEBUG_LABELS[get_debug_mode()]}.")


def apply_settings(lines) -> bool:
    try:
        with db:  # атомарно: при ошибке откатываются все строки
            for ln in lines:
                ln = ln.strip()
                m = SETTINGS_RE.match(ln)
                if m:
                    peer = 2_000_000_000 + int(m.group(1))
                    if not get_chat(peer):
                        raise EditError(f"нет беседы {m.group(1)}")
                    col, val = SETTINGS_COLS[m.group(2).lower()]
                    db.execute(f"UPDATE chats SET {col}=? WHERE peer_id=?", (val, peer))
                    continue
                m = SELFGOV_RE.match(ln)
                if not m:
                    raise EditError(f"не понял строку: {ln}")
                peer = 2_000_000_000 + int(m.group(1))
                if not get_chat(peer):
                    raise EditError(f"нет беседы {m.group(1)}")
                db.execute("UPDATE chats SET self_gov_days=? WHERE peer_id=?", (parse_days(m.group(2)), peer))
    except EditError as e:
        log.info("Настройка отклонена: %s", e)
        return False
    return True


CHATS_PER_PAGE = 6  # inline-клавиатура VK: не больше 10 кнопок и 6 строк (ещё нужны стрелки и кнопка отчёта)
SETTING_FLAGS = {"enabled", "free_run", "admins_only"}
CLOSED_KB = json.dumps({"buttons": [], "inline": True})
SEC, POS, NEG = VkKeyboardColor.SECONDARY, VkKeyboardColor.POSITIVE, VkKeyboardColor.NEGATIVE


def _btn(kb, label: str, color, **payload):
    kb.add_callback_button(label[:40], color, payload=payload)


DELAY_PRESETS = (0, 1, 2, 5, 10, 20, 30, 60)  # минуты


def get_free_run_delay() -> int:
    """Через сколько минут после последней отписи (все ушли) уходит отчёт о свободном беге. Временная настройка."""
    r = db.execute("SELECT value FROM bot_settings WHERE key='free_run_delay'").fetchone()
    return int(r["value"]) if r else FREE_RUN_GRACE_SEC // 60


def fmt_delay(m: int) -> str:
    return "сразу" if m == 0 else f"{m} мин"


def delay_view(pg: int):
    cur = get_free_run_delay()
    text = ("Временная настройка для тестов.\nОтчёт о свободном беге уйдёт администраторам через выбранное время "
            "после того, как все участники ушли и новых отписей нет (проверка раз в 15 секунд).\n"
            f"Сейчас: {fmt_delay(cur)}")
    kb = VkKeyboard(inline=True)
    for i, m in enumerate(DELAY_PRESETS):
        if i and i % 4 == 0:
            kb.add_line()
        _btn(kb, fmt_delay(m), POS if m == cur else SEC, cmd="cs_dset", v=m, pg=pg)
    kb.add_line()
    _btn(kb, "Вернуться", SEC, cmd="cs_dclose")
    return text, kb.get_keyboard()


def chat_num(c) -> int:
    return c["peer_id"] - 2_000_000_000


def main_settings_view(page: int = 0):
    """(текст, клавиатура) общего сообщения: список бесед и кнопки с их названиями."""
    text = render_settings()
    chats = db.execute("SELECT * FROM chats ORDER BY peer_id").fetchall()
    pages = max(1, (len(chats) + CHATS_PER_PAGE - 1) // CHATS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    kb = VkKeyboard(inline=True)
    for i, c in enumerate(chats[page * CHATS_PER_PAGE:(page + 1) * CHATS_PER_PAGE]):
        if i and i % 2 == 0:
            kb.add_line()
        _btn(kb, f"{chat_num(c)}. {c['title'] or 'Без названия'}", SEC, cmd="cs_chat", peer=c["peer_id"], pg=page)
    if pages > 1:
        kb.add_line()
        if page > 0:
            _btn(kb, "◀ Назад", SEC, cmd="cs_page", pg=page - 1)
        if page < pages - 1:
            _btn(kb, "Вперёд ▶", SEC, cmd="cs_page", pg=page + 1)
    if chats:
        kb.add_line()
    _btn(kb, f"Отчёт о беге: {fmt_delay(get_free_run_delay())}", SEC, cmd="cs_delay", pg=page)  # временная настройка
    kb.add_line()
    mode = get_debug_mode()
    _btn(kb, f"Отладка: {DEBUG_SHORT[mode]}", NEG if mode == "off" else SEC, cmd="cs_dbg", pg=page)
    return text, kb.get_keyboard()


def chat_settings_view(c, pg: int):
    """(текст, клавиатура) сообщения с настройками одной беседы."""
    text = "\n".join([
        f"Беседа {chat_num(c)}. {c['title'] or 'Без названия'}",
        f"Учёт баллов: {'включён' if c['enabled'] else 'отключен'}",
        f"Свободный бег: {'включён' if c['free_run'] else 'отключен'}",
        f"Тренировки проводят: {'только администраторы' if c['admins_only'] else 'все участники'}",
        f"День самоуправления: {fmt_days(c['self_gov_days'])}",
    ])
    peer = c["peer_id"]
    kb = VkKeyboard(inline=True)
    _btn(kb, "Бот: включён" if c["enabled"] else "Бот: отключен", POS if c["enabled"] else NEG,
         cmd="cs_tog", f="enabled", peer=peer, pg=pg)
    kb.add_line()
    _btn(kb, "Свободный бег", POS if c["free_run"] else NEG, cmd="cs_tog", f="free_run", peer=peer, pg=pg)
    kb.add_line()
    _btn(kb, "Тренеры: только администраторы" if c["admins_only"] else "Тренеры: все участники", SEC,
         cmd="cs_tog", f="admins_only", peer=peer, pg=pg)
    kb.add_line()
    _btn(kb, f"ДС: {fmt_days(c['self_gov_days'])}", SEC, cmd="cs_days", peer=peer, pg=pg)
    kb.add_line()
    _btn(kb, "Вернуться", SEC, cmd="cs_back", peer=peer, pg=pg)
    return text, kb.get_keyboard()


def days_view(c, pg: int, pc):
    """(текст, клавиатура) выбора дней самоуправления. pc — ID сообщения с настройками беседы (его тоже обновляем)."""
    sel = self_gov_set(c)
    peer = c["peer_id"]
    text = (f"День самоуправления — беседа {chat_num(c)}. {c['title'] or 'Без названия'}\n"
            "В выбранные дни тренировки может проводить любой участник беседы.\n"
            f"Сейчас: {fmt_days(c['self_gov_days'])}")
    kb = VkKeyboard(inline=True)
    _btn(kb, "Отключен", NEG if not sel else SEC, cmd="cs_day", d=-1, peer=peer, pg=pg, pc=pc)
    for d in range(7):
        if d % 4 == 0:
            kb.add_line()
        _btn(kb, DAY_NAMES[d], POS if d in sel else SEC, cmd="cs_day", d=d, peer=peer, pg=pg, pc=pc)
    kb.add_line()
    _btn(kb, "Вернуться", SEC, cmd="cs_dback", peer=peer, pg=pg, pc=pc)
    return text, kb.get_keyboard()


def send_with_kb(uid: int, text: str, kb):
    """Новое сообщение с inline-клавиатурой. Возвращает conversation_message_id или None."""
    args = dict(peer_ids=uid, random_id=random.getrandbits(31), message=text, disable_mentions=1)
    if kb:
        args["keyboard"] = kb
    try:
        r = vk.messages.send(**args)
    except ApiError as e:
        log.warning("Не удалось отправить сообщение %s: %s", uid, e)
        return None
    item = r[0] if r else {}
    if item.get("error"):
        log.warning("Сообщение для %s не доставлено: %s", uid, item["error"])
        return None
    return item.get("conversation_message_id")


def edit_msg(uid: int, cmid, text: str, kb=None) -> bool:
    if not cmid:
        return False
    try:
        vk.messages.edit(peer_id=uid, conversation_message_id=cmid, message=text,
                         keyboard=kb or CLOSED_KB, disable_mentions=1)
        return True
    except ApiError as e:
        log.info("Не удалось отредактировать сообщение %s: %s", cmid, e)
        return False


def delete_msg(uid: int, cmid):
    """Удаляет сообщение бота; если VK не даёт (прошло больше суток) — убирает кнопки и пишет «закрыто»."""
    if not cmid:
        return
    try:
        vk.messages.delete(peer_id=uid, cmids=cmid, delete_for_all=1)
    except ApiError as e:
        log.info("Не удалось удалить сообщение %s (%s) — убираю кнопки", cmid, e)
        edit_msg(uid, cmid, "Закрыто", None)


def show_settings(uid: int, force_new: bool, page: int = 0):
    """
    /настройка → новое сообщение со списком бесед и кнопками. Последующие изменения → редактируем это же
    сообщение; если с его отправки прошло больше 24 часов (или редактирование не удалось) — шлём новое.
    """
    text, kb = main_settings_view(page)
    row = db.execute("SELECT * FROM settings_msgs WHERE user_id=?", (uid,)).fetchone()
    if row and row["cmid"] and not force_new and time.time() - row["sent_ts"] < EDIT_LIMIT_SEC:
        if row["text"] == text:
            return  # ничего не изменилось — VK не даёт «отредактировать» в такой же текст
        if edit_msg(uid, row["cmid"], text, kb):
            db.execute("UPDATE settings_msgs SET text=? WHERE user_id=?", (text, uid))
            db.commit()
            return
    cmid = send_with_kb(uid, text, kb)
    if cmid is None:
        return
    db.execute("INSERT OR REPLACE INTO settings_msgs(user_id,cmid,sent_ts,text) VALUES(?,?,?,?)",
               (uid, cmid, int(time.time()), text))
    db.commit()


def handle_settings_event(obj: dict):
    """Нажатия кнопок настроек (payload.cmd начинается с «cs_»). Только администраторы сообщества."""
    uid, cmid = obj["user_id"], obj.get("conversation_message_id")
    p = obj.get("payload") or {}
    cmd = p.get("cmd")
    if not is_group_admin(uid):
        return answer_event(obj, "Настройки доступны только администраторам сообщества")
    try:
        pg = int(p.get("pg", 0))
        if cmd == "cs_page":
            text, kb = main_settings_view(pg)
            edit_msg(uid, cmid, text, kb)
            return answer_event(obj)
        if cmd == "cs_delay":
            send_with_kb(uid, *delay_view(pg))
            return answer_event(obj)
        if cmd == "cs_dset":
            db.execute("INSERT OR REPLACE INTO bot_settings(key,value) VALUES('free_run_delay',?)", (str(int(p.get("v", 20))),))
            db.commit()
            edit_msg(uid, cmid, *delay_view(pg))
            show_settings(uid, force_new=False, page=pg)
            return answer_event(obj)
        if cmd == "cs_dclose":
            delete_msg(uid, cmid)
            return answer_event(obj)
        if cmd == "cs_dbg":  # владелец → администраторы → выключено → владелец …
            new = DEBUG_MODES[(DEBUG_MODES.index(get_debug_mode()) + 1) % len(DEBUG_MODES)]
            db.execute("INSERT OR REPLACE INTO bot_settings(key,value) VALUES('debug_audience',?)", (new,))
            db.commit()
            show_settings(uid, force_new=False, page=pg)
            return answer_event(obj, f"Отладочные сообщения: {DEBUG_LABELS[new]}")
        peer = int(p.get("peer", 0))
        c = get_chat(peer)
        if not c:
            return answer_event(obj, "Беседа не найдена (возможно, бота исключили)")
        if cmd == "cs_chat":
            send_with_kb(uid, *chat_settings_view(c, pg))
        elif cmd == "cs_tog":
            f = p.get("f")
            if f in SETTING_FLAGS:
                db.execute(f"UPDATE chats SET {f}=1-{f} WHERE peer_id=?", (peer,))
                db.commit()
                edit_msg(uid, cmid, *chat_settings_view(get_chat(peer), pg))
                show_settings(uid, force_new=False, page=pg)
        elif cmd == "cs_days":
            send_with_kb(uid, *days_view(c, pg, cmid))
        elif cmd == "cs_day":
            d = int(p.get("d", -1))
            new = set() if d < 0 else (self_gov_set(c) ^ ({d} if 0 <= d <= 6 else set()))
            db.execute("UPDATE chats SET self_gov_days=? WHERE peer_id=?",
                       (",".join(str(x) for x in sorted(new)), peer))
            db.commit()
            c = get_chat(peer)
            edit_msg(uid, cmid, *days_view(c, pg, p.get("pc")))
            edit_msg(uid, p.get("pc"), *chat_settings_view(c, pg))
            show_settings(uid, force_new=False, page=pg)
        elif cmd in ("cs_back", "cs_dback"):
            delete_msg(uid, cmid)
        answer_event(obj)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка обработки кнопки настроек")
        answer_event(obj, "Не удалось выполнить действие")


def handle_settings(msg: dict) -> bool:
    """True — сообщение относится к настройке и обработано."""
    uid, cmid = msg["from_id"], msg.get("conversation_message_id")
    text = (msg.get("text") or "").strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    is_cmd = bool(CMD_SETTINGS_RE.match(text))
    is_set = bool(lines) and all(SETTINGS_RE.match(l) or SELFGOV_RE.match(l) for l in lines)
    if not (is_cmd or is_set):
        return False
    if not is_group_admin(uid):
        send_dm(uid, "Настройки доступны только администраторам сообщества. "
                     "Обратись к администратору сообщества.")
        return True
    if is_cmd:
        show_settings(uid, force_new=True)
    elif apply_settings(lines):
        react(uid, cmid, "check")
        show_settings(uid, force_new=False)
    else:
        react(uid, cmid, "question")
    return True


def handle_private(msg: dict):
    uid, cmid = msg["from_id"], msg.get("conversation_message_id")
    if uid <= 0:
        return
    ensure_menu(uid)
    if handle_menu(msg):
        return
    if handle_settings(msg):
        return
    lines = [l.strip() for l in (msg.get("text") or "").splitlines() if l.strip()]
    if lines and handle_session_msg(uid, cmid, lines):
        return
    rep = find_report(uid, msg)
    if not rep:
        return
    if not lines:
        return
    try:
        apply_edits(rep["training_id"], lines)
    except EditError as e:
        log.info("Правка отклонена: %s", e)
        react(uid, cmid, "question")
        return
    autocorrect_participants(rep["training_id"])
    update_all_reports(rep["training_id"])  # у всех тренеров / администраторов, получивших отчёт
    react(uid, cmid, "check")


# ───────────────────────── кнопка «Подтвердить» ─────────────────────────
def answer_event(event_obj: dict, text: str = None):
    """Отвечает на нажатие callback-кнопки (без текста — просто гасит «загрузку»)."""
    args = dict(event_id=event_obj["event_id"], user_id=event_obj["user_id"], peer_id=event_obj["peer_id"])
    if text:
        args["event_data"] = json.dumps({"type": "show_snackbar", "text": text})
    try:
        vk.messages.sendMessageEventAnswer(**args)
    except ApiError as e:
        log.warning("sendMessageEventAnswer: %s", e)


def report_rows(tid: int):
    return db.execute("SELECT * FROM reports WHERE training_id=? ORDER BY id", (tid,)).fetchall()


def reply_to_report(uid: int, cmid, text: str):
    """Новое сообщение-ответ на сообщение с отчётом (если его нельзя отредактировать)."""
    args = dict(peer_id=uid, random_id=random.getrandbits(31), message=text, disable_mentions=1)
    if cmid:
        try:
            vk.messages.send(**args, forward=json.dumps(
                {"peer_id": uid, "conversation_message_ids": [cmid], "is_reply": True}))
            return
        except ApiError as e:
            log.info("Не удалось ответить на отчёт (%s) — шлю обычное сообщение", e)
    try:
        vk.messages.send(**args)
    except ApiError as e:
        log.warning("Не удалось отправить сообщение %s: %s", uid, e)


def strip_report_button(rep, by: str = None, rejected: bool = False, reason: str = None) -> bool:
    """Убирает кнопки и дописывает вверху, кто подтвердил (или отклонил) отчёт. False — отредактировать нельзя."""
    if not rep["cmid"] or time.time() - rep["sent_ts"] >= EDIT_LIMIT_SEC:
        return False
    kw = ({"void_reason": reason} if reason
          else {"rejected_by": by} if rejected else {"confirmed_by": by})
    text = render_report(rep["training_id"], note=rep["note"], **kw)
    # сначала явно пустая inline-клавиатура, затем — правка без клавиатуры
    for kb in (json.dumps({"buttons": [], "inline": True}), None):
        args = dict(peer_id=rep["user_id"], conversation_message_id=rep["cmid"], message=text,
                    disable_mentions=1)
        if kb is not None:
            args["keyboard"] = kb
        try:
            vk.messages.edit(**args)
            return True
        except ApiError as e:
            log.info("Не удалось убрать кнопку (%s)", e)
    return False


def handle_report_event(obj: dict):
    """Кнопки правок отчёта (payload.cmd начинается с «rp_»)."""
    uid, cmid = obj["user_id"], obj.get("conversation_message_id")
    p = obj.get("payload") or {}
    cmd, tid = p.get("cmd"), p.get("tid")
    if cmd == "rp_back":  # закрыть сообщение правок / добавления
        delete_msg(uid, cmid)
        clear_session(uid, cmid)
        return answer_event(obj)
    t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    if not t:
        return answer_event(obj, "Тренировка не найдена")
    if uid not in {r["user_id"] for r in report_rows(tid)}:
        return answer_event(obj, "Править отчёт может только тот, кому он отправлен")
    if t["status"] == "confirmed":
        return answer_event(obj, "Уже подтверждено")
    if t["status"] == "rejected":
        return answer_event(obj, "Отчёт уже отклонён")
    try:
        k, n = p.get("k"), int(p.get("n") or 0)
        if cmd == "rp_ok":  # «Подтвердить правки»: пометки бота снимаются, участник считается проверенным
            row = get_entry(tid, "p", n) if k == "p" and t["kind"] == "free_run" else None
            if not row:
                return answer_event(obj, "Не найден (возможно, уже удалён)")
            db.execute("UPDATE participants SET flag=NULL, src=NULL, verified=1 WHERE id=?", (row["id"],))
            db.commit()
            update_all_reports(tid)  # отчёт у всех получателей без пометок
            edit_msg(uid, cmid, entry_text(tid, "p", n), entry_kb(tid, "p", n))  # кнопка исчезает
            return answer_event(obj, "Правки подтверждены, пометки сняты")
        if cmd in ("rp_rej", "rp_rej_no"):
            rep_row = db.execute("SELECT * FROM reports WHERE training_id=? AND user_id=? AND cmid=?",
                                 (tid, uid, cmid)).fetchone()
            if cmd == "rp_rej":  # первый клик: просим подтвердить (прямо в этом же сообщении)
                text = REJECT_WARN + "\n\n" + render_report(tid, note=rep_row["note"] if rep_row else None)
                if not edit_msg(uid, cmid, text, reject_kb(tid)):  # сообщению больше суток — отдельное сообщение
                    send_with_kb(uid, REJECT_WARN, reject_kb(tid))
            elif rep_row:  # «Отмена»: возвращаем обычный вид отчёта
                update_report(rep_row)
            else:  # «Отмена» в отдельном сообщении подтверждения
                delete_msg(uid, cmid)
            return answer_event(obj)
        if cmd == "rp_rej_yes":  # второй клик: отклоняем
            db.execute("UPDATE trainings SET status='rejected' WHERE id=?", (tid,))
            db.commit()
            for ss in db.execute("SELECT * FROM edit_sessions WHERE training_id=?", (tid,)).fetchall():
                delete_msg(ss["user_id"], ss["cmid"])
            db.execute("DELETE FROM edit_sessions WHERE training_id=?", (tid,))
            db.commit()
            rejecter = user_link(uid)
            reps = report_rows(tid)
            if cmid not in {r["cmid"] for r in reps}:
                delete_msg(uid, cmid)  # отдельное сообщение подтверждения больше не нужно
            for rep in reps:  # кнопки убираются во ВСЕХ отправленных сообщениях с отчётом
                if not strip_report_button(rep, rejecter, rejected=True):
                    reply_to_report(rep["user_id"], rep["cmid"],
                                    f"✖ Отчёт отклонил(а) {rejecter}, в таблицу он не записан")
            return answer_event(obj, "Отчёт отклонён, в таблицу не записан")
        if cmd == "rp_page":
            rep_row = db.execute("SELECT * FROM reports WHERE training_id=? AND user_id=? AND cmid=?",
                                 (tid, uid, cmid)).fetchone()
            if rep_row:
                db.execute("UPDATE reports SET page=? WHERE id=?", (int(p.get("pg", 0)), rep_row["id"]))
                db.commit()
                update_report(db.execute("SELECT * FROM reports WHERE id=?", (rep_row["id"],)).fetchone())
        elif cmd == "rp_sel":
            text = entry_text(tid, k, n)
            if not text:
                return answer_event(obj, "Не найден (возможно, уже удалён)")
            open_session(uid, tid, k, n, text, entry_kb(tid, k, n))
        elif cmd == "rp_time":
            kb = VkKeyboard(inline=True)
            kb.add_callback_button("Вернуться", VkKeyboardColor.SECONDARY, payload={"cmd": "rp_back"})
            open_session(uid, tid, "time", None, TIME_TEXT, kb.get_keyboard())
        elif cmd == "rp_add":
            kb = VkKeyboard(inline=True)
            kb.add_callback_button("Вернуться", VkKeyboardColor.SECONDARY, payload={"cmd": "rp_back"})
            open_session(uid, tid, "add", None, ADD_TEXT_FR if t["kind"] == "free_run" else ADD_TEXT,
                         kb.get_keyboard())
        elif cmd == "rp_del":
            ensure_trainers(t)
            if k == "t":
                if len(trainer_rows(tid)) <= 1:
                    return answer_event(obj, "Нельзя удалить единственного тренера")
                db.execute("DELETE FROM trainers WHERE training_id=? AND num=?", (tid, n))
            else:
                db.execute("DELETE FROM participants WHERE training_id=? AND num=?", (tid, n))
            db.commit()
            delete_msg(uid, cmid)
            clear_session(uid, cmid)
            update_all_reports(tid)
        answer_event(obj)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка обработки кнопки правок")
        answer_event(obj, "Не удалось выполнить действие")


def handle_event(obj: dict):
    payload = obj.get("payload") or {}
    if str(payload.get("cmd", "")).startswith("cs_"):
        return handle_settings_event(obj)
    if str(payload.get("cmd", "")).startswith("rp_"):
        return handle_report_event(obj)
    if payload.get("cmd") != "confirm":
        return
    tid = payload.get("tid")
    t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    if not t:
        return answer_event(obj, "Тренировка не найдена")
    uid = obj["user_id"]
    reps = report_rows(tid)
    if uid not in {r["user_id"] for r in reps}:  # подтвердить может любой, кому отправлен отчёт
        return answer_event(obj, "Подтвердить может только тот, кому отправлен отчёт")
    if t["status"] == "confirmed":
        return answer_event(obj, "Уже подтверждено")
    if t["status"] == "rejected":
        return answer_event(obj, "Отчёт уже отклонён")
    if not write_to_google_sheet(tid):
        return answer_event(obj, "Не удалось записать в таблицу. Подробности в логе бота — попробуй ещё раз позже")
    db.execute("UPDATE trainings SET status='confirmed' WHERE id=?", (tid,))
    db.commit()
    answer_event(obj, "Записано в таблицу ✔")
    for ss in db.execute("SELECT * FROM edit_sessions WHERE training_id=?", (tid,)).fetchall():
        delete_msg(ss["user_id"], ss["cmid"])
    db.execute("DELETE FROM edit_sessions WHERE training_id=?", (tid,))
    db.commit()

    confirmer = user_link(uid)
    d_part, t_part = training_label(t)
    done_text = f"Отчёт о тренировке {d_part} {t_part} {'(индивидуальная)' if t['kind'] == 'individual' else 'в беседе ' + chat_title(t['peer_id'])} записан"
    done_sent = False
    for rep in reps:  # кнопка убирается во ВСЕХ отправленных сообщениях с отчётом
        edited = strip_report_button(rep, confirmer)
        if rep["user_id"] == uid:
            if not done_sent:
                if edited:
                    send_dm(uid, done_text)
                else:
                    reply_to_report(uid, rep["cmid"], done_text)
                done_sent = True
        elif not edited:
            reply_to_report(rep["user_id"], rep["cmid"], f"✔ Запись по этому отчёту подтвердил(а) {confirmer}")


# ───────────────────────── удалённые сообщения ─────────────────────────
def void_training(tid: int, reason: str):
    """Тренировка/забег не засчитывается: статус «отклонена», кнопки отчётов убираются, причина пишется вверху."""
    db.execute("UPDATE trainings SET status='rejected' WHERE id=?", (tid,))
    for ss in db.execute("SELECT * FROM edit_sessions WHERE training_id=?", (tid,)).fetchall():
        delete_msg(ss["user_id"], ss["cmid"])
    db.execute("DELETE FROM edit_sessions WHERE training_id=?", (tid,))
    db.commit()
    for rep in report_rows(tid):
        if not strip_report_button(rep, reason=reason):
            reply_to_report(rep["user_id"], rep["cmid"], f"✖ Отчёт отменён: {reason}. В таблицу он не записан")


def revert_stop(t) -> bool:
    """Сообщение «/стоп» удалили: тренировка снова идёт, отправленные отчёты отзываются. False — вернуть нельзя."""
    cur = active_training(t["peer_id"])
    if cur and cur["id"] != t["id"]:
        log.warning("Удалён /стоп тренировки %s, но в беседе уже идёт другая — тренировку не возвращаю", t["id"])
        return False
    tid = t["id"]
    for ss in db.execute("SELECT * FROM edit_sessions WHERE training_id=?", (tid,)).fetchall():
        delete_msg(ss["user_id"], ss["cmid"])
    db.execute("DELETE FROM edit_sessions WHERE training_id=?", (tid,))
    for rep in report_rows(tid):
        if not strip_report_button(rep, reason="удалено сообщение с /стоп, тренировка продолжается"):
            reply_to_report(rep["user_id"], rep["cmid"], "✖ Отчёт отменён: удалено сообщение с /стоп")
    db.execute("DELETE FROM reports WHERE training_id=?", (tid,))
    db.execute("UPDATE participants SET leave_ts=NULL, auto_leave=0 WHERE training_id=? AND auto_leave=1", (tid,))
    db.execute("UPDATE trainers SET leave_ts=NULL WHERE training_id=? AND leave_cmid IS NULL AND leave_ts=?",
               (tid, t["end_ts"]))
    db.execute("UPDATE trainings SET status='active', end_ts=NULL, stopper_id=NULL, stop_cmid=NULL WHERE id=?", (tid,))
    db.commit()
    return True


def command_deleted(peer: int, cmid):
    """
    Сообщение с командой удалили: его действие снимается, пока отчёт не подтверждён. То, что уже записано в
    таблицу, не трогаем — о таком только сообщаем владельцу (поправить вручную).
    """
    log.info("Удалено сообщение с командой (беседа %s, сообщение %s) — снимаю его действие", peer, cmid)
    db.execute("DELETE FROM free_run_msgs WHERE peer_id=? AND cmid=?", (peer, cmid))
    db.commit()
    recorded, refresh = [], set()

    # свободный бег
    if db.execute(
            "SELECT 1 FROM participants p JOIN trainings t ON t.id=p.training_id WHERE t.peer_id=? "
            "AND t.kind='free_run' AND t.status='confirmed' AND (p.join_cmid=? OR p.leave_cmid=? OR p.deduct_cmid=?)",
            (peer, cmid, cmid, cmid)).fetchone():
        recorded.append("забег свободного бега")
    touched = []
    free_run_undo(peer, cmid, allow_sent=True, touched=touched)
    for tid in dict.fromkeys(touched):
        if db.execute("SELECT COUNT(*) FROM participants WHERE training_id=?", (tid,)).fetchone()[0] == 0:
            void_training(tid, "все записи забега удалены вместе с сообщениями")
        else:
            refresh.add(tid)

    # /старт и /стоп групповой тренировки
    for t in db.execute("SELECT * FROM trainings WHERE peer_id=? AND kind='group' AND (start_cmid=? OR stop_cmid=?)",
                        (peer, cmid, cmid)).fetchall():
        if t["status"] == "confirmed":
            recorded.append("тренировка " + " ".join(training_label(t)))
        elif t["start_cmid"] == cmid and t["status"] in ("active", "finished"):
            void_training(t["id"], "удалено сообщение с /старт")
        elif t["stop_cmid"] == cmid and t["status"] == "finished":
            revert_stop(t)

    # #лапа / #уход участников и «+ тренер» / #уход тренеров
    for p in db.execute(
            "SELECT p.*, t.status AS tstatus, t.end_ts AS tend FROM participants p JOIN trainings t "
            "ON t.id=p.training_id WHERE t.peer_id=? AND t.kind='group' AND (p.join_cmid=? OR p.leave_cmid=?)",
            (peer, cmid, cmid)).fetchall():
        if p["tstatus"] == "confirmed":
            recorded.append(f"участник {p['name']}")
            continue
        if p["tstatus"] not in ("active", "finished"):
            continue
        if p["join_cmid"] == cmid:
            db.execute("DELETE FROM participants WHERE id=?", (p["id"],))
        else:
            if p["tstatus"] == "active":
                db.execute("UPDATE participants SET leave_ts=NULL, leave_cmid=NULL WHERE id=?", (p["id"],))
            else:  # тренировка закрыта: без #уход участник уходит в момент окончания
                db.execute("UPDATE participants SET leave_ts=?, leave_cmid=NULL, auto_leave=1 WHERE id=?",
                           (p["tend"], p["id"]))
            react(peer, p["join_cmid"], "eye")
        if p["tstatus"] == "finished":
            refresh.add(p["training_id"])
    for r in db.execute(
            "SELECT r.*, t.status AS tstatus, t.end_ts AS tend, t.start_cmid AS tstart FROM trainers r "
            "JOIN trainings t ON t.id=r.training_id WHERE t.peer_id=? AND t.kind='group' "
            "AND (r.join_cmid=? OR r.leave_cmid=?)", (peer, cmid, cmid)).fetchall():
        if r["tstatus"] == "confirmed":
            recorded.append("тренер тренировки")
            continue
        if r["tstatus"] not in ("active", "finished"):
            continue
        if r["join_cmid"] == cmid:
            if cmid == r["tstart"]:
                continue  # это /старт — тренировка уже отменена выше
            db.execute("DELETE FROM trainers WHERE id=?", (r["id"],))
        elif r["tstatus"] == "active":
            db.execute("UPDATE trainers SET leave_ts=NULL, leave_cmid=NULL WHERE id=?", (r["id"],))
        else:
            db.execute("UPDATE trainers SET leave_ts=?, leave_cmid=NULL WHERE id=?", (r["tend"], r["id"]))
        if r["tstatus"] == "finished":
            refresh.add(r["training_id"])
    db.commit()

    for tid in refresh:
        t = db.execute("SELECT status FROM trainings WHERE id=?", (tid,)).fetchone()
        if t and t["status"] == "finished":
            update_all_reports(tid)
    if recorded:
        send_debug(("del", peer, cmid),
                   f"🗑 Удалено сообщение с командой, но оно уже учтено в записанном отчёте "
                   f"({', '.join(dict.fromkeys(recorded))}).\nБеседа: {chat_title(peer)} (№{peer - 2_000_000_000}), "
                   f"сообщение №{cmid}. Таблицу бот не меняет — поправьте запись вручную.")


def poll_deleted_messages():
    """
    VK не присылает боту событие об удалении сообщения, поэтому раз в DELETE_POLL_SEC секунд проверяем недавние
    сообщения с командами: свежие — каждый раз, старые — раз в 10 минут. Пропавшее сообщение считается удалённым
    после двух проверок подряд (защита от случайного сбоя VK).
    """
    now = int(time.time())
    rows = db.execute(
        "SELECT * FROM msg_log WHERE gone=0 AND added_ts>? AND ((added_ts>? AND checked_ts<?) OR checked_ts<?) "
        "ORDER BY peer_id, cmid", (now - DELETE_WATCH_SEC, now - 7200, now - DELETE_POLL_SEC + 5, now - 600)).fetchall()
    by_peer = {}
    for r in rows:
        by_peer.setdefault(r["peer_id"], []).append(r)
    for peer, items_ in by_peer.items():
        for i in range(0, len(items_), 100):
            chunk = items_[i:i + 100]
            try:
                resp = vk.messages.getByConversationMessageId(
                    peer_id=peer, conversation_message_ids=",".join(str(w["cmid"]) for w in chunk))
            except ApiError as e:
                log.warning("Проверка удалений: не удалось прочитать сообщения беседы %s: %s", peer, e)
                break
            found = {it.get("conversation_message_id") for it in resp.get("items", [])}
            need = 3 if not found and len(chunk) >= 3 else 2  # «пропало всё сразу» — скорее сбой, ждём дольше
            for w in chunk:
                if w["cmid"] in found:
                    db.execute("UPDATE msg_log SET miss=0, checked_ts=? WHERE peer_id=? AND cmid=?",
                               (now, peer, w["cmid"]))
                    continue
                miss = w["miss"] + 1
                gone = 1 if miss >= need else 0
                db.execute("UPDATE msg_log SET miss=?, checked_ts=?, gone=? WHERE peer_id=? AND cmid=?",
                           (miss, now, gone, peer, w["cmid"]))
                db.commit()
                if gone:
                    try:
                        command_deleted(peer, w["cmid"])
                    except Exception:  # noqa: BLE001
                        log.exception("Ошибка обработки удалённого сообщения %s/%s", peer, w["cmid"])
            db.commit()


# ───────────────────────── резервная копия базы ─────────────────────────
# Основная база лежит на постоянном диске (DATA_DIR, см. выше). Параллельно её содержимое раз в BACKUP_INTERVAL_SEC
# секунд (если что-то изменилось) сохраняется в Google Таблицу — лист «Резерв бота». Если хостинг стёр диск
# (обновление, пересоздание контейнера) и база при запуске пустая, бот восстанавливает данные из этого листа.
BACKUP_TABLES = ("chats", "bot_settings", "trainings", "participants", "trainers", "reports",
                 "settings_msgs", "free_run_msgs", "menu_state", "edit_sessions")
_backup = {"enabled": False, "hash": None, "changes": -1, "ts": 0.0, "thread": None, "retry_ts": 0.0}


def build_snapshot():
    """(строка base64 со сжатым JSON всех таблиц, число значимых записей: беседы + тренировки + настройки)."""
    data, meaningful = {"v": 1, "tables": {}}, 0
    for tbl in BACKUP_TABLES:
        cur = db.execute(f"SELECT * FROM {tbl} ORDER BY rowid")
        rows = [list(r) for r in cur.fetchall()]
        data["tables"][tbl] = {"cols": [d[0] for d in cur.description], "rows": rows}
        if tbl in ("chats", "bot_settings", "trainings"):
            meaningful += len(rows)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii"), meaningful


def restore_snapshot(blob: str) -> int:
    """Загружает копию в базу (INSERT OR REPLACE по ключам; незнакомые столбцы пропускаются). Возвращает число строк."""
    data = json.loads(zlib.decompress(base64.b64decode(blob)).decode("utf-8"))
    total = 0
    with db:
        for tbl in BACKUP_TABLES:
            d = data.get("tables", {}).get(tbl)
            if not d:
                continue
            have = {r["name"] for r in db.execute(f"PRAGMA table_info({tbl})")}
            cols = [c for c in d["cols"] if c in have]
            idx = [d["cols"].index(c) for c in cols]
            sql = f"INSERT OR REPLACE INTO {tbl}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
            for row in d["rows"]:
                db.execute(sql, [row[i] for i in idx])
                total += 1
    return total


def db_is_fresh() -> bool:
    return build_snapshot()[1] == 0


def startup_restore():
    """При запуске: если база пуста, а в таблице есть резервная копия — восстанавливаем. Включает резервирование."""
    _backup["retry_ts"] = time.time()
    fresh = db_is_fresh()
    if not BACKUP_TO_SHEET:
        return
    try:
        import sheets
        got = sheets.read_backup()
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось прочитать резервную копию из таблицы: %s", e)
        if not fresh:
            _backup["enabled"] = True  # база не пуста — копировать можно, затирать резерв пустотой нельзя
        return
    if fresh and got:
        n = restore_snapshot(got[0])
        _names.clear()
        log.warning("База была пуста — ДАННЫЕ ВОССТАНОВЛЕНЫ из резервной копии в таблице (%s записей, копия от %s)",
                    n, got[1].get("saved_at", "?"))
    elif fresh:
        log.info("База пуста, резервной копии в таблице нет — начинаем с чистого листа")
    else:
        log.info("База данных не пуста — восстановление не требуется")
    _backup["enabled"] = True


def backup_tick(force: bool = False):
    """Раз в BACKUP_INTERVAL_SEC секунд (если база менялась) отправляет копию в таблицу (в фоне, не тормозит бота)."""
    if not BACKUP_TO_SHEET:
        return
    now = time.time()
    if not _backup["enabled"]:
        if force or now - _backup["retry_ts"] < 300:
            return
        startup_restore()
        if not _backup["enabled"]:
            return
    if not force and (now - _backup["ts"] < BACKUP_INTERVAL_SEC or db.total_changes == _backup["changes"]):
        return
    th = _backup["thread"]
    if th is not None and th.is_alive():
        if not force:
            return
        th.join(30)
    blob, meaningful = build_snapshot()
    _backup["ts"], _backup["changes"] = now, db.total_changes
    if meaningful == 0:
        return  # пустую базу в резерв не пишем
    h = hashlib.sha256(blob.encode("ascii")).hexdigest()
    if h == _backup["hash"]:
        return

    def work():
        try:
            import sheets
            sheets.write_backup(blob, {"saved_at": datetime.now(MSK).strftime("%d.%m.%Y %H:%M:%S"),
                                       "records": meaningful})
            _backup["hash"] = h
            log.info("Резервная копия базы записана в таблицу")
        except Exception as e:  # noqa: BLE001
            _backup["changes"] = -1  # повторить при следующей проверке
            log.warning("Не удалось сохранить резервную копию в таблицу: %s", e)

    th = threading.Thread(target=work, daemon=True)
    _backup["thread"] = th
    th.start()
    if force:
        th.join(30)


# ───────────────────────── «Отчёты в обработке» (меню администратора) ─────────────────────────
def report_recipients_text(t) -> str:
    """Кому отправлен отчёт: всем администраторам или конкретному тренеру (с именами)."""
    reps = latest_reports(t["id"])
    if not reps:
        return "отчёт никому не доставлен (получатель не разрешил сообщения от сообщества)"
    names = ", ".join(user_link(r["user_id"]) for r in reps)
    if t["kind"] == "free_run":
        return f"всем администраторам сообщества (подтвердить может любой): {names}"
    note = next((r["note"] for r in reps if r["note"]), None)
    if note:
        why = ("в день самоуправления тренировку провёл не администратор беседы" if "самоуправ" in note
               else "тренеру доставить не удалось")
        return f"администраторам сообщества ({why}): {names}"
    return f"{'тренеру' if len(reps) == 1 else 'тренерам'}: {names}"


def pending_messages() -> list:
    """Тексты сообщений: тренировки/забеги в процессе и отчёты, ожидающие подтверждения."""
    members = get_members()
    running, waiting = [], []
    for t in db.execute("SELECT * FROM trainings WHERE status IN ('active','run','finished') "
                        "ORDER BY COALESCE(end_ts, start_ts), id").fetchall():
        peer_txt = (f"Беседа: {chat_title(t['peer_id'])} (№{t['peer_id'] - 2_000_000_000})"
                    if t["kind"] != "individual" else "Индивидуальная тренировка")
        total = db.execute("SELECT COUNT(*) FROM participants WHERE training_id=?", (t["id"],)).fetchone()[0]
        inside = db.execute("SELECT COUNT(*) FROM participants WHERE training_id=? AND leave_ts IS NULL",
                            (t["id"],)).fetchone()[0]
        if t["status"] == "active":
            ensure_trainers(t)
            who_ = ", ".join(trainer_link(r, trainer_view(r, members)[0]) for r in trainer_rows(t["id"]))
            running.append("\n".join([
                f"▶ Тренировка идёт — начало {dt(t['start_ts']):%d.%m.%Y %H:%M}", f"   {peer_txt}",
                f"   Тренеры: {who_}", f"   Участников отмечено: {total} (сейчас на тренировке: {inside})"]))
        elif t["status"] == "run":
            running.append("\n".join([
                f"🏃 Свободный бег идёт — с {dt(t['start_ts']):%d.%m.%Y %H:%M}", f"   {peer_txt}",
                f"   Участников: {total} (ещё бегут: {inside})",
                f"   Отчёт уйдёт всем администраторам сообщества через {fmt_delay(get_free_run_delay())} "
                "после ухода последнего участника"]))
        else:
            d_part, t_part = training_label(t)
            title = {"free_run": "Свободный бег", "individual": "Индивидуальная тренировка"}.get(t["kind"], "Тренировка")
            waiting.append("\n".join([
                f"📝 {title} {d_part}, {t_part} — ждёт подтверждения", f"   {peer_txt}",
                f"   Участников: {total}", f"   Отчёт отправлен: {report_recipients_text(t)}"]))
    if not running and not waiting:
        return ["Незаписанных отчётов и тренировок в процессе нет ✔"]
    blocks = [f"Отчёты в обработке: {len(running) + len(waiting)}"]
    n = 0
    for title, items_ in (("Идут сейчас (отчёта ещё нет):", running), ("Отчёт отправлен, не подтверждён:", waiting)):
        if items_:
            blocks.append(title)
            for it in items_:
                n += 1
                blocks.append(f"{n}. {it}")
    parts, cur = [], ""
    for b in blocks:  # сообщение VK — до 4096 символов
        if cur and len(cur) + len(b) + 2 > 3500:
            parts.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    return parts + [cur]


def show_pending(uid: int):
    for part in pending_messages():
        send_dm(uid, part)


# ───────────────────────── запуск ─────────────────────────
def dispatch_event(event):
    """Разбор одного события Long Poll."""
    try:
        etype = str(getattr(event.type, "value", event.type))
        if LOG_EVENTS and etype != "message_new":
            log.info("Событие Long Poll: %s", etype)
        if etype == "message_new":
            msg = event.message
            if msg["peer_id"] >= 2_000_000_000:
                handle_chat(msg)
            else:
                handle_private(msg)
        elif etype == "message_event":
            handle_event(event.object)
            ensure_menu(event.object.get("user_id", 0))
        elif etype == "message_edit":
            obj = event.object
            msg = obj.get("message", obj)  # в разных версиях API объект бывает «голым» или во вложении
            log.info("Получено событие message_edit: беседа %s, сообщение %s",
                     msg.get("peer_id"), msg.get("conversation_message_id"))
            if msg.get("peer_id", 0) >= 2_000_000_000:
                handle_chat_edit(msg)
    except Exception:  # одно событие не должно ронять бота
        log.exception("Ошибка обработки события")


def check_longpoll_settings():
    """Читает настройки Long Poll сообщества и предупреждает, если не включено нужное."""
    try:
        st = vk.groups.getLongPollSettings(group_id=GROUP_ID)
    except ApiError as e:
        log.warning("Не удалось прочитать настройки Long Poll (%s). Нужен ключ с правом «управление "
                    "сообществом». Проверьте вручную: Управление → Работа с API → Long Poll API", e)
        return
    ev = st.get("events", {})
    log.info("Long Poll: включён=%s, версия API=%s, message_new=%s, message_edit=%s, message_event=%s",
             st.get("is_enabled"), st.get("api_version"), ev.get("message_new"),
             ev.get("message_edit"), ev.get("message_event"))
    if not st.get("is_enabled"):
        log.warning("Long Poll API в сообществе ВЫКЛЮЧЕН")
    if not ev.get("message_edit"):
        log.warning("В Long Poll не включено событие «Редактирование сообщения» (message_edit): "
                    "правки #лапа будут ловиться только запасным опросом раз в %s с", EDIT_POLL_SEC)
    if not ev.get("message_event"):
        log.warning("В Long Poll не включено событие message_event — кнопка «Подтвердить» не заработает")


def main():
    global vk_session, vk
    if not TOKEN or not GROUP_ID:
        sys.exit("Задай VK_TOKEN и VK_GROUP_ID (см. .env.example)")
    vk_session = vk_api.VkApi(token=TOKEN, api_version="5.199")
    vk = vk_session.get_api()

    if "--assets" in sys.argv:
        print(json.dumps(vk_session.method("messages.getReactionsAssets"), ensure_ascii=False, indent=2))
        return

    log.info("База данных: %s", os.path.abspath(DB_PATH) if DB_PATH != ":memory:" else DB_PATH)
    if os.path.isdir("/app/data") and not os.path.abspath(DB_PATH).startswith("/app/data"):
        log.warning("Каталог /app/data существует, но база лежит вне его — при обновлении/перезапуске "
                    "хостинга она может стереться. Задайте переменную DATA_DIR=/app/data")
    for k, v in REACTIONS.items():
        if v is None:
            log.warning("REACTION '%s' не настроена — реакция ставиться не будет", k)

    check_longpoll_settings()
    startup_restore()
    atexit.register(lambda: _backup["enabled"] and backup_tick(force=True))  # последняя копия при остановке
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        try:
            lp = VkBotLongPoll(vk_session, GROUP_ID, wait=10)
            log.info("Бот запущен (опрос правок: %s)",
                     f"каждые {EDIT_POLL_SEC} с" if EDIT_POLL_SEC > 0 else "выключен")
            last_poll = last_tick = last_del = 0.0
            while True:
                for event in lp.check():
                    dispatch_event(event)
                if time.time() - last_tick >= 15:
                    last_tick = time.time()
                    try:
                        free_run_tick()
                    except Exception:
                        log.exception("Ошибка учёта свободного бега")
                    try:
                        backup_tick()
                    except Exception:
                        log.exception("Ошибка резервного копирования")
                if EDIT_POLL_SEC > 0 and time.time() - last_poll >= EDIT_POLL_SEC:
                    last_poll = time.time()
                    try:
                        poll_watched_messages()
                    except Exception:
                        log.exception("Ошибка опроса правок")
                if DELETE_POLL_SEC > 0 and time.time() - last_del >= DELETE_POLL_SEC:
                    last_del = time.time()
                    try:
                        poll_deleted_messages()
                    except Exception:
                        log.exception("Ошибка проверки удалённых сообщений")
        except Exception:
            log.exception("Long Poll упал, перезапуск через 5 секунд")
            time.sleep(5)


if __name__ == "__main__":
    main()
