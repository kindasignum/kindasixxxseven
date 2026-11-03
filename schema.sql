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
    sent_ts     INTEGER NOT NULL,
    note        TEXT,                          -- пояснение вверху отчёта (почему отчёт пришёл именно вам)
    page        INTEGER NOT NULL DEFAULT 0     -- страница кнопок правок в этом сообщении (по 5 записей)
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
    added_ts    INTEGER,
    self_gov_days TEXT NOT NULL DEFAULT ''     -- дни самоуправления «0,2,5» (пн=0 … вс=6), пусто — отключено
);

-- Последнее сообщение с настройками для каждого администратора (его бот редактирует)
CREATE TABLE IF NOT EXISTS settings_msgs(
    user_id INTEGER PRIMARY KEY,
    cmid    INTEGER,
    sent_ts INTEGER,
    text    TEXT
);

-- Тренеры тренировки (можно несколько): «+ тренер», «#уход», правки отчёта
CREATE TABLE IF NOT EXISTS trainers(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    training_id INTEGER NOT NULL,
    num         INTEGER NOT NULL,              -- постоянный номер тренера в отчёте
    vk_id       INTEGER,                       -- NULL — добавлен вручную без страницы ВК
    name        TEXT,                          -- NULL — взять из листа «Участники» / из ВК
    join_ts     INTEGER NOT NULL,
    leave_ts    INTEGER,
    join_cmid   INTEGER,
    leave_cmid  INTEGER
);

-- Журнал команд беседы: нужен для «/старт» задним числом и опроса правок
CREATE TABLE IF NOT EXISTS msg_log(
    peer_id    INTEGER NOT NULL,
    cmid       INTEGER NOT NULL,
    from_id    INTEGER,
    date       INTEGER,
    text       TEXT,
    recognized INTEGER NOT NULL DEFAULT 1,     -- 0 — команда не сработала, следим за правками
    added_ts   INTEGER NOT NULL,
    PRIMARY KEY (peer_id, cmid)
);

-- Открытое сообщение правок отчёта в ЛС (выбранный участник/тренер или режим добавления).
-- Одна запись на человека: следующие его сообщения «имя …», «время …» или «Имя 15:00-16:30» применяются к ней
CREATE TABLE IF NOT EXISTS edit_sessions(
    user_id     INTEGER PRIMARY KEY,           -- кто правит отчёт
    training_id INTEGER NOT NULL,
    kind        TEXT NOT NULL,                 -- p — участник, t — тренер, add — добавление
    num         INTEGER,                       -- номер участника/тренера в отчёте (для add — NULL)
    cmid        INTEGER,                       -- сообщение бота с кнопками правок
    ts          INTEGER NOT NULL
);

-- Добавлено в более старые таблицы (бот делает это сам при запуске):
--   ALTER TABLE chats   ADD COLUMN self_gov_days TEXT NOT NULL DEFAULT '';   -- дни самоуправления «0,2,5» (пн=0)
--   ALTER TABLE reports ADD COLUMN note TEXT;                                -- пояснение вверху отчёта
--   ALTER TABLE reports ADD COLUMN page INTEGER NOT NULL DEFAULT 0;          -- страница кнопок правок
