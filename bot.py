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
<<<<<<< HEAD
=======
import os
print("B64_EXISTS:", bool(os.getenv("GOOGLE_CREDENTIALS_BASE64")))
print("B64_LEN:", len(os.getenv("GOOGLE_CREDENTIALS_BASE64", "")))

>>>>>>> c46da25c8ce711c7fdbe19aa5fc9b80d1953bbfb
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import vk_api
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
<<<<<<< HEAD
# База хранится в DATA_DIR (на хостинге — постоянный диск, например /app/data) или в DB_PATH
DATA_DIR = os.getenv("DATA_DIR", "").strip()
DB_PATH = os.getenv("DB_PATH") or (os.path.join(DATA_DIR, "trainings.db") if DATA_DIR else "trainings.db")
if DB_PATH != ":memory:" and os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
=======
DB_PATH = os.getenv("DB_PATH", "trainings.db")
>>>>>>> c46da25c8ce711c7fdbe19aa5fc9b80d1953bbfb
MSK = ZoneInfo("Europe/Moscow")
EDIT_LIMIT_SEC = 24 * 3600  # VK разрешает редактировать сообщения сутки
JOIN_TOLERANCE_SEC = 6 * 3600  # насколько раньше старта допускается время прихода при правках
# новые беседы по умолчанию включены (0 — выключены до команды «N вкл»)
DEFAULT_CHAT_ENABLED = int(os.getenv("DEFAULT_CHAT_ENABLED", "1"))
# запасной список админов сообщества (если VK не отдаёт список): VK ID через запятую
ENV_ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("BOT_ADMIN_IDS", ""))}


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
    status TEXT NOT NULL DEFAULT 'active',  -- active | finished | confirmed
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
    sent_ts INTEGER NOT NULL
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
    added_ts INTEGER
);
"""
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
    """30 мин = 0,5 балла, допуск ±15 мин (25→0,5; 45→1; 75→1,5)."""
    return ((minutes + 15) // 30) * 0.5


def fmt_points(p: float) -> str:
    return f"{p:g}".replace(".", ",")


def fmt_dur(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} ч {m} минут"
    if h:
        return f"{h} ч"
    return f"{m} минут"


def minutes_of(p) -> int:
    return max(0, (p["leave_ts"] - p["join_ts"]) // 60)


def fmt_t(ts: int, start_ts: int) -> str:
    """ЧЧ:ММ; если время выпало на другие сутки, чем старт тренировки, — добавляет (дд.мм)."""
    d = dt(ts)
    s = f"{d:%H:%M}"
    if d.date() != dt(start_ts).date():
        s += f" ({d:%d.%m})"
    return s


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


_group_admins = {"ts": 0.0, "ids": set()}


def is_group_admin(uid: int) -> bool:
    """Создатель или администратор сообщества (+ запасной список BOT_ADMIN_IDS)."""
    if uid in ENV_ADMIN_IDS:
        return True
    if time.time() - _group_admins["ts"] > 300:
        ids = set()
        try:
            r = vk.groups.getMembers(group_id=GROUP_ID, filter="managers")
            for it in r.get("items", []):
                if isinstance(it, dict):
                    if it.get("role") in ("creator", "administrator"):
                        ids.add(it["id"])
                else:
                    ids.add(it)
        except ApiError as e:
            log.warning("Не удалось получить руководителей сообщества: %s "
                        "(можно задать BOT_ADMIN_IDS в .env)", e)
        _group_admins.update(ts=time.time(), ids=ids)
    return uid in _group_admins["ids"]


# ───────────────────────── Google Таблица ─────────────────────────
def build_training_payload(training_id: int) -> dict:
    """
    Данные тренировки для sheets.write_training(). CW ID тренера берётся с листа «Участники»
    по VK ID («ID вк» → «ID вар»), CW ID участника — по имени («Имя на варе»).
    """
    t = db.execute("SELECT * FROM trainings WHERE id=?", (training_id,)).fetchone()
    members = get_members()
    lead_name, lead_cw, _ = trainer_info(members, t["leader_id"])
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
    return {
        "key": f"{t['peer_id']}-{t['start_ts']}",
        "start": dt(t["start_ts"]), "end": dt(t["end_ts"]),
        "chat_id": t["peer_id"] - 2_000_000_000, "chat_title": chat_title(t["peer_id"]),
        "leader_id": t["leader_id"], "leader_name": lead_name, "leader_cw_id": lead_cw,
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
        res = sheets.write_training(build_training_payload(training_id))
        log.info("Запись в таблицу, тренировка %s: %s", training_id, res)
        return True
    except sheets.SheetsError as e:
        log.error("Запись в таблицу не удалась: %s", e)
        return False


# ───────────────────────── отчёт ─────────────────────────
REPORT_HELP = (
    "Проверь, что всё правильно. Если время присоединения к тренировке или ухода "
    "с неё отличается от реального, отправь ответным сообщением номер участника "
    "и настоящее время присутствия на тренировке. Например:\n\n"
    "1 15:00-16:30\n\n"
    "Если тренировка шла через полночь, просто укажи время, например 23:30-00:45 — "
    "бот сам определит дату.\n\n"
    "Если в отчёте есть лишний участник или кого-то не хватает, отправь ответным "
    "сообщением «+» или «-», имя участника (или его номер) и время участия, если "
    "есть. Например:\n\n"
    "+ Лихушек 15:00-16:30\n"
    "- Лихушек\n\n"
    "Если имя участника записано неверно, исправь его:\n\n"
    "1 имя Лихушек\n\n"
    "Когда всё будет верно, нажми «Подтвердить»."
)


def render_report(tid: int) -> str:
    t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    s, e = dt(t["start_ts"]), dt(t["end_ts"])
    parts = db.execute(
        "SELECT * FROM participants WHERE training_id=? ORDER BY num", (tid,)
    ).fetchall()

    members = get_members()
    lead_name, _, lead_found = trainer_info(members, t["leader_id"])
    unmatched = []

    def line(p):
        m = minutes_of(p)
        warn = ""
        if members and not members.participant(p["name"], p["vk_id"]):
            warn = " ⚠"
            unmatched.append(p["num"])
        return (
            f"{p['num']}. {who(p)}{warn} {fmt_t(p['join_ts'], t['start_ts'])} - "
            f"{fmt_t(p['leave_ts'], t['start_ts'])}, "
            f"{fmt_dur(m)}, {fmt_points(calc_points(m))} б."
        )

    if s.date() == e.date():
        head = f"Тренировка {s:%d.%m.%Y}. {s:%H:%M}-{e:%H:%M}"
    else:  # тренировка перешла через полночь
        head = f"Тренировка {s:%d.%m.%Y}–{e:%d.%m.%Y}. {s:%H:%M}-{e:%H:%M}"
    own = [line(p) for p in parts if not p["auto_leave"]]
    auto = [line(p) for p in parts if p["auto_leave"]]

    out = [
        head,
        f"Беседа: {chat_title(t['peer_id'])}",
        "",
        f"Вёл тренировку: [id{t['leader_id']}|{lead_name}]",
        "",
        "Участники:",
        *(own or ["—"]),
    ]
    if auto:
        out += ["", "Не зафиксирован самостоятельный уход:", *auto]
    notes = []
    if unmatched:
        notes.append("⚠ — персонаж не найден на листе «Участники», CW ID не будет записан. "
                     "Если имя написано с ошибкой, исправь его: «N имя Верное Имя».")
    if members and not lead_found:
        notes.append("⚠ Тренер не найден на листе «Участники» по VK ID — CW ID тренера останется пустым.")
    if notes:
        out += ["", *notes]
    out += ["", REPORT_HELP]
    return "\n".join(out)


def confirm_kb(tid: int) -> str:
    kb = VkKeyboard(inline=True)
    kb.add_callback_button(
        "Подтвердить", VkKeyboardColor.POSITIVE, payload={"cmd": "confirm", "tid": tid}
    )
    return kb.get_keyboard()


def send_new_report(tid: int, user_ids):
    text, kb = render_report(tid), confirm_kb(tid)
    for uid in user_ids:
        if not uid:
            continue
        try:
            r = vk.messages.send(
                peer_ids=uid,
                random_id=random.getrandbits(31),
                message=text,
                keyboard=kb,
                disable_mentions=1,
            )
        except ApiError as e:
            log.warning("Не удалось отправить отчёт %s: %s", uid, e)
            continue
        item = r[0] if r else {}
        if item.get("error"):
            log.warning("Отчёт для %s не доставлен: %s (нужно разрешить сообщения от сообщества)",
                        uid, item["error"])
            continue
        db.execute(
            "INSERT INTO reports(training_id,user_id,message_id,cmid,sent_ts) VALUES(?,?,?,?,?)",
            (tid, uid, item.get("message_id"), item.get("conversation_message_id"), int(time.time())),
        )
        db.commit()
        return uid
    log.error("Отчёт по тренировке %s никому не отправлен", tid)
    return None


def update_report(rep):
    tid, uid = rep["training_id"], rep["user_id"]
    if time.time() - rep["sent_ts"] < EDIT_LIMIT_SEC:
        try:
            vk.messages.edit(
                peer_id=uid,
                conversation_message_id=rep["cmid"],
                message=render_report(tid),
                keyboard=confirm_kb(tid),
                disable_mentions=1,
            )
            return
        except ApiError as e:
            log.info("Редактирование недоступно (%s) — шлю новое сообщение", e)
    send_new_report(tid, [uid])  # новое сообщение становится «исходным»


# ───────────────────────── обработка бесед ─────────────────────────
MENTION_RE = re.compile(r"^\s*\[club\d+\|[^\]]*\]\s*,?\s*", re.I)
# команда может стоять в любом месте сообщения, рядом может быть другой текст
# после команды можно указать время: «/старт 10:00», «/стоп в 12:00»
CLOCK = r"(?:[ \t]+(?:в[ \t]+)?(\d{1,2})[:.](\d{2})(?!\d))?"
RE_START = re.compile(r"(?<![\w/])/старт(?!\w)" + CLOCK, re.I)
RE_STOP = re.compile(r"(?<![\w/])/стоп(?!\w)" + CLOCK, re.I)
RE_JOIN = re.compile(r"^#лапа\s+(.+)$", re.I | re.S)
RE_LEAVE = re.compile(r"^#уход\s+(.+)$", re.I | re.S)


def active_training(peer_id: int):
    return db.execute(
        "SELECT * FROM trainings WHERE peer_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (peer_id,),
    ).fetchone()


def next_num(tid: int) -> int:
    r = db.execute("SELECT COALESCE(MAX(num),0)+1 AS n FROM participants WHERE training_id=?", (tid,))
    return r.fetchone()["n"]


def handle_chat(msg: dict):
    peer, uid, cmid = msg["peer_id"], msg["from_id"], msg.get("conversation_message_id")
    if msg.get("action"):
        return handle_action(peer, msg["action"])
    if uid <= 0:
        return
    register_chat(peer)
    text = MENTION_RE.sub("", msg.get("text") or "").strip()
    if not text or ("/" not in text and "#" not in text):
        return
    chat = get_chat(peer)
    if not chat or not chat["enabled"]:
        return  # беседа отключена в /настройка — бот её игнорирует
    # TODO (свободный бег): if chat["free_run"]: ... — доработаем позже
    ts = floor_min(msg["date"])
    ensure_admin(peer)
    tr = active_training(peer)

    # /старт
    ms = RE_START.search(text)
    if ms:
        if tr:
            return
        if chat["admins_only"] and not is_chat_admin(peer, uid):
            return  # в беседе тренировки ведут только администраторы
        # «/старт 10:00» — тренировка считается начатой в 10:00, даже если команда отправлена раньше/позже
        start_ts = (resolve_clock(ts, ms.group(1), ms.group(2)) if ms.group(1) else None) or ts
        db.execute(
            "INSERT INTO trainings(peer_id,leader_id,start_ts,start_cmid) VALUES(?,?,?,?)",
            (peer, uid, start_ts, cmid),
        )
        db.commit()
        react(peer, cmid, "up")
        return

    if not tr:
        return  # вне тренировки команды игнорируются

    # #лапа Имя (кличка)
    m = RE_JOIN.match(text)
    if m:
        name = norm_name(m.group(1))
        if not name:
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
        return

    # #уход Имя
    m = RE_LEAVE.match(text)
    if m:
        name = norm_name(m.group(1))
        if not name:
            return
        base = "SELECT * FROM participants WHERE training_id=? AND leave_ts IS NULL "
        p = db.execute(base + "AND vk_id=? AND name=? ORDER BY id LIMIT 1", (tr["id"], uid, name)).fetchone()
        if not p:  # ушёл другой человек, а написал, например, ведущий
            p = db.execute(base + "AND name=? ORDER BY id LIMIT 1", (tr["id"], name)).fetchone()
        if not p:  # имя написано иначе, но у отправителя ровно одна активная запись
            rows = db.execute(base + "AND vk_id=?", (tr["id"], uid)).fetchall()
            p = rows[0] if len(rows) == 1 else None
        if not p:
            return
        db.execute(
            "UPDATE participants SET leave_ts=?, leave_cmid=? WHERE id=?", (max(ts, p["join_ts"]), cmid, p["id"])
        )
        db.commit()
        react(peer, cmid, "check")
        react(peer, p["join_cmid"], "check")  # 👁 → ✔ на сообщении #лапа
        return

    # /стоп
    mst = RE_STOP.search(text)
    if mst:
        if chat["admins_only"] and not is_chat_admin(peer, uid):
            return
        end = ts
        if mst.group(1):  # «/стоп 12:00» — конец тренировки в 12:00, даже если команда пришла позже
            end = resolve_clock(ts, mst.group(1), mst.group(2), not_before=tr["start_ts"]) or ts
        end = max(end, tr["start_ts"])
        # всё, что было «после конца» (вход/уход, записанные до получения /стоп), подтягиваем к концу
        db.execute("UPDATE participants SET join_ts=? WHERE training_id=? AND join_ts>?", (end, tr["id"], end))
        db.execute("UPDATE participants SET leave_ts=? WHERE training_id=? AND leave_ts>?", (end, tr["id"], end))
        # кто не ушёл сам — уходит в момент окончания тренировки
        db.execute(
            "UPDATE participants SET leave_ts=?, auto_leave=1 WHERE training_id=? AND leave_ts IS NULL",
            (end, tr["id"]),
        )
        db.execute(
            "UPDATE trainings SET status='finished', end_ts=?, stopper_id=? WHERE id=?",
            (end, uid, tr["id"]),
        )
        db.commit()
        chat_title(peer, refresh=True)  # актуальное название на момент отчёта
        send_new_report(tr["id"], [tr["leader_id"], uid])
        return


# ───────────────────────── правки в ЛС ─────────────────────────
class EditError(Exception):
    pass


TIME = r"(\d{1,2})[:.](\d{2})"
RE_RENAME = re.compile(r"^\[?(\d+)\]?\s+имя\s+(.+)$", re.I)
RE_FIX = re.compile(rf"^\[?(\d+)\]?\s+{TIME}\s*[-–—]\s*{TIME}$")
RE_PM = re.compile(rf"^([+\-])\s*(.+?)(?:\s+{TIME}\s*[-–—]\s*{TIME})?$")


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
    j = next((c for c in _candidates(start, h1, m1) if c >= start - JOIN_TOLERANCE_SEC), None)
    l = next((c for c in _candidates(start, h2, m2) if j is not None and c > j), None)
    if j is None or l is None or j > end or l > end + JOIN_TOLERANCE_SEC:
        raise EditError("время вне тренировки")
    return j, l


def apply_edits(tid: int, lines):
    t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    with db:  # атомарно: при ошибке откат всех строк
        for ln in lines:
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
                else:
                    cur = db.execute(
                        "DELETE FROM participants WHERE training_id=? AND name=?",
                        (tid, norm_name(arg)),
                    )
                if cur.rowcount == 0:
                    raise EditError(f"не найден: {arg}")


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
        "WHERE r.user_id=? AND t.status!='confirmed' ORDER BY r.id DESC LIMIT 1",
        (uid,),
    ).fetchone()


# ───────────────────────── /настройка (ЛС сообщества) ─────────────────────────
CMD_SETTINGS_RE = re.compile(r"^/настройка(?!\w)", re.I)
SETTINGS_RE = re.compile(r"^(\d+)\s+(вкл|выкл|сб\+|сб-|админы\+|админы-)$", re.I)
SETTINGS_COLS = {
    "вкл": ("enabled", 1), "выкл": ("enabled", 0),
    "сб+": ("free_run", 1), "сб-": ("free_run", 0),
    "админы+": ("admins_only", 1), "админы-": ("admins_only", 0),
}
SETTINGS_HELP = (
    "Чтобы изменить настройки, введи номер беседы и параметр (можно несколько строк):\n"
    "• вкл/выкл — подключить или отключить учёт баллов в беседе\n"
    "• сб+/сб- — подключить или отключить учёт свободного бега\n"
    "• админы+/админы- — подключить проведение тренировок только администраторами "
    "или всеми участниками\n\n"
    "Например: 1 вкл"
)


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
    lines = ["Активные беседы:"]
    chats = db.execute("SELECT * FROM chats ORDER BY peer_id").fetchall()
    for c in chats:
        cid, title = c["peer_id"] - 2_000_000_000, c["title"] or "Без названия"
        if c["enabled"]:
            lines.append(
                f"{cid} {title} — включён. "
                f"Свободный бег: {'включён' if c['free_run'] else 'отключен'}. "
                f"Тренировки проводят: {'только администраторы' if c['admins_only'] else 'все участники'}"
            )
        else:
            lines.append(f"{cid} {title} — отключен")
    if not chats:
        lines.append("— пока нет бесед (бот увидит беседу после первого сообщения в ней)")
    return "\n".join(lines + ["", SETTINGS_HELP])


def apply_settings(lines) -> bool:
    try:
        with db:  # атомарно: при ошибке откатываются все строки
            for ln in lines:
                m = SETTINGS_RE.match(ln.strip())
                peer = 2_000_000_000 + int(m.group(1))
                if not get_chat(peer):
                    raise EditError(f"нет беседы {m.group(1)}")
                col, val = SETTINGS_COLS[m.group(2).lower()]
                db.execute(f"UPDATE chats SET {col}=? WHERE peer_id=?", (val, peer))
    except EditError as e:
        log.info("Настройка отклонена: %s", e)
        return False
    return True


def show_settings(uid: int, force_new: bool):
    """
    /настройка → новое сообщение со списком. Последующие правки → редактируем это же сообщение;
    если с его отправки прошло больше 24 часов (или редактирование не удалось) — шлём новое.
    """
    text = render_settings()
    row = db.execute("SELECT * FROM settings_msgs WHERE user_id=?", (uid,)).fetchone()
    if row and row["cmid"] and not force_new and time.time() - row["sent_ts"] < EDIT_LIMIT_SEC:
        if row["text"] == text:
            return  # ничего не изменилось — VK не даёт «отредактировать» в такой же текст
        try:
            vk.messages.edit(peer_id=uid, conversation_message_id=row["cmid"],
                             message=text, disable_mentions=1)
            db.execute("UPDATE settings_msgs SET text=? WHERE user_id=?", (text, uid))
            db.commit()
            return
        except ApiError as e:
            log.info("Не удалось отредактировать сообщение настроек (%s) — шлю новое", e)
    try:
        r = vk.messages.send(peer_ids=uid, random_id=random.getrandbits(31),
                             message=text, disable_mentions=1)
    except ApiError as e:
        log.warning("Не удалось отправить настройки %s: %s", uid, e)
        return
    item = r[0] if r else {}
    if item.get("error"):
        log.warning("Настройки для %s не доставлены: %s", uid, item["error"])
        return
    db.execute("INSERT OR REPLACE INTO settings_msgs(user_id,cmid,sent_ts,text) VALUES(?,?,?,?)",
               (uid, item.get("conversation_message_id"), int(time.time()), text))
    db.commit()


def handle_settings(msg: dict) -> bool:
    """True — сообщение относится к настройке и обработано."""
    uid, cmid = msg["from_id"], msg.get("conversation_message_id")
    text = (msg.get("text") or "").strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    is_cmd = bool(CMD_SETTINGS_RE.match(text))
    is_set = bool(lines) and all(SETTINGS_RE.match(l) for l in lines)
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
    if handle_settings(msg):
        return
    rep = find_report(uid, msg)
    if not rep:
        return
    lines = [l.strip() for l in (msg.get("text") or "").splitlines() if l.strip()]
    if not lines:
        return
    try:
        apply_edits(rep["training_id"], lines)
    except EditError as e:
        log.info("Правка отклонена: %s", e)
        react(uid, cmid, "question")
        return
    update_report(rep)
    react(uid, cmid, "check")


# ───────────────────────── кнопка «Подтвердить» ─────────────────────────
def answer_event(event_obj: dict, text: str):
    try:
        vk.messages.sendMessageEventAnswer(
            event_id=event_obj["event_id"],
            user_id=event_obj["user_id"],
            peer_id=event_obj["peer_id"],
            event_data=json.dumps({"type": "show_snackbar", "text": text}),
        )
    except ApiError as e:
        log.warning("sendMessageEventAnswer: %s", e)


def handle_event(obj: dict):
    payload = obj.get("payload") or {}
    if payload.get("cmd") != "confirm":
        return
    tid = payload.get("tid")
    t = db.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    if not t:
        return answer_event(obj, "Тренировка не найдена")
    if obj["user_id"] not in (t["leader_id"], t["stopper_id"]):
        return answer_event(obj, "Подтвердить может только ведущий")
    if t["status"] == "confirmed":
        return answer_event(obj, "Уже подтверждено")
    if write_to_google_sheet(tid):
        db.execute("UPDATE trainings SET status='confirmed' WHERE id=?", (tid,))
        db.commit()
        answer_event(obj, "Записано в таблицу ✔")
    else:
        answer_event(obj, "Не удалось записать в таблицу. Подробности в логе бота — попробуй ещё раз позже")


# ───────────────────────── запуск ─────────────────────────
def main():
    global vk_session, vk
    if not TOKEN or not GROUP_ID:
        sys.exit("Задай VK_TOKEN и VK_GROUP_ID (см. .env.example)")
    vk_session = vk_api.VkApi(token=TOKEN, api_version="5.199")
    vk = vk_session.get_api()

    if "--assets" in sys.argv:
        print(json.dumps(vk_session.method("messages.getReactionsAssets"), ensure_ascii=False, indent=2))
        return

<<<<<<< HEAD
    log.info("База данных: %s", os.path.abspath(DB_PATH) if DB_PATH != ":memory:" else DB_PATH)
    if os.path.isdir("/app/data") and not os.path.abspath(DB_PATH).startswith("/app/data"):
        log.warning("Каталог /app/data существует, но база лежит вне его — при обновлении/перезапуске "
                    "хостинга она может стереться. Задайте переменную DATA_DIR=/app/data")
=======
>>>>>>> c46da25c8ce711c7fdbe19aa5fc9b80d1953bbfb
    for k, v in REACTIONS.items():
        if v is None:
            log.warning("REACTION '%s' не настроена — реакция ставиться не будет", k)

    while True:
        try:
            lp = VkBotLongPoll(vk_session, GROUP_ID)
            log.info("Бот запущен")
            for event in lp.listen():
                try:
                    if event.type == VkBotEventType.MESSAGE_NEW:
                        msg = event.message
                        if msg["peer_id"] >= 2_000_000_000:
                            handle_chat(msg)
                        else:
                            handle_private(msg)
                    elif event.type == VkBotEventType.MESSAGE_EVENT:
                        handle_event(event.object)
                except Exception:  # одно сообщение не должно ронять бота
                    log.exception("Ошибка обработки события")
        except Exception:
            log.exception("Long Poll упал, перезапуск через 5 секунд")
            time.sleep(5)


if __name__ == "__main__":
    main()
