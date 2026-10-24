-- Схема базы данных бота (SQLite). Совпадает с тем, что создаёт bot.py.
-- Ручное создание:  sqlite3 trainings.db < schema.sql

CREATE TABLE IF NOT EXISTS trainings(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id     INTEGER NOT NULL,                  -- ID беседы (2000000000+N)
    leader_id   INTEGER NOT NULL,                  -- кто написал /старт
    stopper_id  INTEGER,                           -- кто написал /стоп
    start_ts    INTEGER NOT NULL,                  -- unix-время начала (UTC)
    end_ts      INTEGER,                           -- unix-время окончания
    status      TEXT NOT NULL DEFAULT 'active',    -- active | finished | confirmed
    start_cmid  INTEGER                            -- номер сообщения /старт в беседе
);

CREATE TABLE IF NOT EXISTS participants(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    training_id INTEGER NOT NULL,
    num         INTEGER NOT NULL,                  -- постоянный номер в отчёте
    vk_id       INTEGER,                           -- NULL, если добавлен вручную через «+»
    name        TEXT NOT NULL,
    join_ts     INTEGER NOT NULL,
    leave_ts    INTEGER,
    join_cmid   INTEGER,                           -- сообщение #лапа
    leave_cmid  INTEGER,                           -- сообщение #уход
    auto_leave  INTEGER NOT NULL DEFAULT 0         -- 1 = ушёл по /стоп
);

CREATE TABLE IF NOT EXISTS reports(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    training_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,                  -- кому отправлен отчёт (ЛС)
    message_id  INTEGER,
    cmid        INTEGER,
    sent_ts     INTEGER NOT NULL
);

-- Необязательные индексы (ускоряют поиск при большом объёме данных)
CREATE INDEX IF NOT EXISTS idx_tr_peer_status ON trainings(peer_id, status);
CREATE INDEX IF NOT EXISTS idx_part_training  ON participants(training_id, num);
CREATE INDEX IF NOT EXISTS idx_rep_user       ON reports(user_id, cmid);

-- Настройки бесед (команда /настройка в ЛС сообщества)
CREATE TABLE IF NOT EXISTS chats(
    peer_id     INTEGER PRIMARY KEY,
    title       TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,    -- учёт баллов в беседе
    free_run    INTEGER NOT NULL DEFAULT 0,    -- свободный бег (задел)
    admins_only INTEGER NOT NULL DEFAULT 0,    -- тренировки ведут только админы беседы
    added_ts    INTEGER
);

-- Последнее сообщение с настройками для каждого администратора (его бот редактирует)
CREATE TABLE IF NOT EXISTS settings_msgs(
    user_id INTEGER PRIMARY KEY,
    cmid    INTEGER,
    sent_ts INTEGER,
    text    TEXT
);
