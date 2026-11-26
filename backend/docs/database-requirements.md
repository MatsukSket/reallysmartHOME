# Требования к базе данных для решателя задач

Исторический документ этапа проектирования. Текущая реализация SQLite описана в [корневом README](../../README.md).

Документ составлен по фактическому коду решателя (`backend/app`), а не по
проектным соображениям: перечислены ровно те данные, которые решатель
читает и записывает.

## Кратко

Решатель уже работает полностью, но данные держит в памяти. Нужно
заменить этот слой на базу, не меняя остальной код. Для этого есть
готовый интерфейс — порт `StoragePort` в `app/ports.py`. Всё, что ниже,
описывает, какие таблицы за этим интерфейсом стоят.

Решатель обращается к данным только через этот интерфейс. Если он
реализован, остальной код менять не придётся.

На этом этапе для проекта предполагалась SQLite. Схема ниже относится к исходным требованиям. Если планируется PostgreSQL,
скажи: там доступны JSONB и массивы, и часть решений упростится.

## Важно: порт покрывает не всё

Сейчас `StoragePort` описывает только четыре группы данных: комнаты,
устройства, целевые значения и историю событий. Остальное решатель
держит в других объектах, и в базу это пока не попадает:

| Данные | Где лежат сейчас | Теряются при перезапуске |
|---|---|---|
| Комнаты, устройства, цели, история | `MemoryStorage` | да |
| Расписания и пары моментов | объект `Solver` | да |
| Блокировки устройств | объект `Solver` | да |
| Ожидающие подтверждения команды | объект `Solver` | да |
| Пользователи, приглашения, устройства входа | `AuthService` | да |
| Журнал действий | `Journal` | да |

Чтобы всё это переживало перезапуск, порт нужно расширить. Ниже описаны
таблицы для всех групп, а в разделе «Что нужно от тебя» — функции,
которые понадобится добавить.

Отдельно: в порт не входит функция `history_contains`, хотя решатель её
вызывает (проверка повторов событий). Её тоже нужно добавить.

## Таблицы

### Справочник дома

```sql
CREATE TABLE rooms (
  id          TEXT PRIMARY KEY,               -- 'kitchen', 'bedroom'
  name        TEXT NOT NULL UNIQUE,           -- 'Кухня'
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE devices (
  id               TEXT PRIMARY KEY,          -- 'light_kitchen'
  name             TEXT NOT NULL,             -- 'Свет на кухне'
  type             TEXT NOT NULL,             -- 'light', 'heater', 'sensor_temperature'
  room_id          TEXT NOT NULL REFERENCES rooms(id),
  state            TEXT NOT NULL DEFAULT '{}',-- JSON: свойства зависят от типа
  error_code       TEXT,                      -- 'E10', NULL если ошибки нет
  last_event_time  TEXT,                      -- время события по часам модели
  received_at      TEXT,                      -- время получения события решателем
  created_at       TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (room_id, name)
);
CREATE INDEX devices_room_idx ON devices(room_id);
```

Три решения, которые стоит объяснить.

**Состояние хранится как JSON.** У разных типов разные свойства: у света
`power` и `brightness`, у котла `power` и `coolant_temperature`, у
пылесоса `mode`, `charge`, `place`. Отдельная таблица свойств потребовала
бы соединения при каждом чтении устройства, а читается оно постоянно.
Полный список свойств по типам — в `app/house.yaml`.

**Две отметки времени, а не одна.** `last_event_time` — когда событие
произошло по часам модели, `received_at` — когда решатель его получил.
Неактуальность данных считается по второй: часы модели и сервера могут
расходиться. Это не дублирование. Обе допускают `NULL`: у только что
запущенной системы событий ещё не было.

**`error_code` отдельным полем, а не внутри JSON.** По нему идёт проверка
при каждой команде: устройство в ошибке — команда отклоняется, пока не
выполнен сброс.

### Целевые значения

```sql
CREATE TABLE targets (
  room_id         TEXT NOT NULL REFERENCES rooms(id),
  parameter       TEXT NOT NULL,              -- 'temperature', 'humidity'
  value           REAL NOT NULL,              -- 22.0
  device_id       TEXT NOT NULL REFERENCES devices(id),
  base_value      REAL,                       -- к чему вернуться после расписания
  base_device_id  TEXT REFERENCES devices(id),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (room_id, parameter)
);
```

Ключ — пара «комната и параметр»: в одной комнате одна целевая
температура и одна целевая влажность. `base_value` хранит обычное
значение, к которому расписание возвращает параметр после интервала.

### Расписания

```sql
CREATE TABLE schedules (
  id           TEXT PRIMARY KEY,              -- 'sch-1'
  kind         TEXT NOT NULL CHECK (kind IN ('command', 'target')),
  at_time      TEXT NOT NULL,                 -- '07:00'
  days         TEXT NOT NULL,                 -- JSON-массив: '[0,1,2,3,4]', понедельник = 0
  device_id    TEXT REFERENCES devices(id),   -- для kind = 'command'
  operation    TEXT,
  params       TEXT NOT NULL DEFAULT '{}',    -- JSON
  room_id      TEXT REFERENCES rooms(id),     -- для kind = 'target'
  parameter    TEXT,
  value        REAL,
  source_pair  TEXT,                          -- связь с расписанием целевого значения
  next_due     TEXT,                          -- ближайшее срабатывание
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX schedules_due_idx ON schedules(next_due) WHERE enabled = 1;

CREATE TABLE schedule_pairs (
  id          TEXT PRIMARY KEY,               -- 'pair-1'
  room_id     TEXT NOT NULL REFERENCES rooms(id),
  parameter   TEXT NOT NULL,
  value       REAL NOT NULL,
  at_time     TEXT NOT NULL,                  -- к какому времени
  until_time  TEXT NOT NULL,                  -- до какого времени
  days        TEXT NOT NULL,
  base_value  REAL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**Почему два вида расписаний в одной таблице.** Расписание команды
(«в 19:00 включить свет») и расписание целевого значения («к 7:00 цель
25 градусов») — это моменты «время → действие». Различается только
действие. Один запрос «что наступило» дешевле двух.

**`next_due` — хранимое поле, а не вычисляемое.** Фоновая работа раз в
секунду спрашивает «есть ли расписания со сроком не позже сейчас».
Индекс по этому полю делает выборку мгновенной независимо от общего
числа расписаний. После срабатывания поле пересчитывается решателем.

**`schedule_pairs` отдельно, хотя моменты лежат в `schedules`.**
Пользователь задаёт пару «к 7:00 / до 9:00», решатель превращает её в два
момента со сдвигом на 20 минут назад. Но при удалении пользователь должен
видеть одно расписание, а не два. Пара хранит исходный запрос,
`source_pair` связывает моменты обратно.

**`days` — JSON-массив, а не битовая маска.** Маска компактнее, но при
разборе ошибок и в отладке массив читается лучше. На десятках расписаний
разница в размере несущественна.

### История событий и журнал

```sql
CREATE TABLE events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id    TEXT NOT NULL,                 -- без внешнего ключа, см. ниже
  kind         TEXT NOT NULL,                 -- 'reading', 'state', 'error',
                                              -- 'cleaning_finished', 'rejected'
  value        TEXT,                          -- JSON: число, строка или объект
  code         TEXT,
  occurred_at  TEXT NOT NULL,
  received_at  TEXT NOT NULL DEFAULT (datetime('now')),
  command_id   TEXT,
  reason       TEXT
);
CREATE UNIQUE INDEX events_dedup_idx
  ON events(device_id, kind, occurred_at, COALESCE(command_id, ''));
CREATE INDEX events_device_idx ON events(device_id, occurred_at DESC);
CREATE INDEX events_time_idx ON events(occurred_at DESC);

CREATE TABLE journal (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  impact_id  TEXT NOT NULL,                   -- 'imp-448ec094e078'
  at         TEXT NOT NULL DEFAULT (datetime('now')),
  module     TEXT NOT NULL,                   -- 'управление устройствами'
  action     TEXT NOT NULL,                   -- 'команда завершена'
  subject    TEXT,                            -- 'light_kitchen'
  status     TEXT,                            -- 'выполнено', 'отклонено'
  reason     TEXT,
  actor      TEXT                             -- 'admin'
);
CREATE INDEX journal_impact_idx ON journal(impact_id);
CREATE INDEX journal_at_idx ON journal(at DESC);
```

**Внешнего ключа на `devices` в истории нет намеренно.** Туда попадают и
отброшенные сообщения от неизвестных устройств с причиной «устройство не
зарегистрировано». Это нужно для разбора: если модель присылает мусор,
должно быть видно, что именно пришло. Внешний ключ такие записи бы не
пропустил.

**Уникальный индекс для поиска повторов.** Повтором считается событие с
тем же устройством, тем же видом и тем же временем. Если модель вернула
номер команды, он тоже входит в сравнение: две разные команды могли
выполниться в одну секунду. Отсюда `COALESCE(command_id, '')` — иначе
два `NULL` в SQLite считаются разными, и повтор без номера команды не
отловится. Проверка повтора есть в коде, но уникальный индекс — надёжная
страховка на случай гонки.

**Журнал и история — разные вещи.** История показывает, что происходило с
домом. Журнал — что делал сам решатель: кто, что, когда, с каким
статусом. `impact_id` связывает записи одной обработки: видно всю цепочку
от запроса до итога.

### Пользователи, приглашения, вход

```sql
CREATE TABLE users (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  username       TEXT NOT NULL UNIQUE,
  role           TEXT NOT NULL CHECK (role IN ('resident', 'admin')),
  password_hash  TEXT NOT NULL,               -- 'соль$хэш', формат уже готов
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE invites (
  code_hash   TEXT PRIMARY KEY,               -- SHA-256 от кода, не сам код
  username    TEXT NOT NULL,
  role        TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  used_at     TEXT,                           -- NULL, пока не использовано
  created_by  TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE sessions (
  id            TEXT PRIMARY KEY,             -- 'c43c80828891b25a'
  username      TEXT NOT NULL REFERENCES users(username),
  refresh_hash  TEXT NOT NULL UNIQUE,         -- SHA-256 от долгого пропуска
  device        TEXT NOT NULL DEFAULT 'браузер',
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  last_used_at  TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at    TEXT NOT NULL,
  admin_until   TEXT,                         -- режим администратора
  revoked_at    TEXT                          -- отзыв устройства
);
CREATE INDEX sessions_user_idx ON sessions(username) WHERE revoked_at IS NULL;
```

**Долгий пропуск хранится хэшем.** В коде он лежит открытым текстом,
потому что живёт в памяти и никуда не уходит. В базе хранить его открытым
нельзя: утечка базы означала бы вход на все устройства. Хватит SHA-256 —
пропуск длинный и случайный, перебирать его бессмысленно.

**`admin_until` привязан к устройству, а не к короткому пропуску.** Иначе
режим администратора сбрасывался бы каждые полчаса вместе с обновлением
пропуска, и пароль пришлось бы вводить заново. Это важное решение, его
стоит сохранить.

**Выход отзывает оба пропуска.** Если отозвать только короткий, долгий
молча выдаст новый. Поэтому `revoked_at` в `sessions`, а не удаление
строки — так остаётся след для разбора.

**Короткие пропуска в базу не идут.** Они живут полчаса и создаются при
каждом обновлении. Хранить их — лишняя нагрузка без пользы.

### Состояние решателя

```sql
CREATE TABLE device_locks (
  device_id   TEXT PRIMARY KEY REFERENCES devices(id),
  property    TEXT NOT NULL,                  -- 'power', 'brightness'
  value       TEXT NOT NULL,                  -- JSON: что поставил пользователь
  until       TEXT NOT NULL,
  impact_id   TEXT,
  actor       TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX device_locks_until_idx ON device_locks(until);

CREATE TABLE pending_commands (
  model_id    TEXT PRIMARY KEY,               -- 'cmd-17'
  device_id   TEXT NOT NULL REFERENCES devices(id),
  expected    TEXT NOT NULL DEFAULT '{}',     -- JSON
  deadline    TEXT NOT NULL,
  source      TEXT NOT NULL,                  -- 'manual', 'target', 'schedule'
  operation   TEXT NOT NULL,
  impact_id   TEXT,
  actor       TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**Зачем блокировки в базе.** Сейчас они в памяти и теряются при
перезапуске: человек вручную выключил обогреватель, сервер перезапустился,
автоматика снова его включила. Это заметный дефект. В базе блокировка
переживает перезапуск.

**Зачем ожидающие команды.** При перезапуске команда, ждавшая
подтверждения, сейчас просто исчезает. По базе можно при старте найти
такие строки и честно пометить их «не подтверждено» в журнале.

## Что нужно от тебя: интерфейс

Решатель вызывает эти функции. Точные имена и сигнатуры — в
`app/ports.py`, здесь смысл.

**Комнаты**
- `rooms()` — все комнаты
- `room(id)` — одна комната или ничего

**Устройства**
- `devices(room_id=None)` — все или только в комнате
- `device(id)` — одно или ничего
- `save_device(device)` — сохранить состояние, ошибку и обе отметки времени

**Целевые значения**
- `targets(room_id=None)` — все или по комнате
- `save_target(target)` — сохранить, заменяя прежнее для той же комнаты и параметра
- `delete_target(room_id, parameter)`

**История и журнал**
- `append_history(record)`, `read_history(limit)`
- `history_contains(device_id, kind, moment, command_id=None)` — проверка повтора
- `journal_write(record)`, `journal_read(limit, impact_id=None)`

**Расписания** (нужно добавить в порт)
- `schedules()` — все моменты
- `save_schedule(moment)` — создать или обновить, включая `next_due`
- `delete_schedule(id)`
- `due_schedules(now)` — **ключевая функция**: моменты, у которых `next_due <= now` и `enabled`. Индекс по `next_due` делает её мгновенной
- `pairs()`, `save_pair(pair)`, `delete_pair(id)`

**Пользователи и вход** (нужно добавить в порт)
- `user_by_name(username)`, `create_user(...)`, `users()`, `update_password(username, hash)`
- `create_invite(...)`, `invite_by_code_hash(hash)`, `mark_invite_used(hash, at)`
- `create_session(...)`, `session_by_refresh_hash(hash)`, `session_by_id(id)`
- `sessions_by_user(username)`, `touch_session(id, last_used_at, expires_at)`
- `set_admin_until(id, moment)`, `revoke_session(id, at)`, `revoke_user_sessions(username, except_id)`

**Блокировки и ожидающие команды** (нужно добавить в порт)
- `get_lock(device_id)`, `set_lock(lock)`, `delete_lock(device_id)`, `expired_locks(now)`
- `pending_commands()`, `save_pending(pending)`, `delete_pending(model_id)`

## Требования, которые легко упустить

**Транзакции.** Выполнение команды и запись в журнал — одна транзакция.
Иначе при сбое получится команда без записи или запись без команды.

**Отдача копий.** Решатель меняет объекты в памяти, а потом вызывает
`save_*`. Если возвращать ссылки на общие объекты, изменения протекут мимо
транзакций. Слой должен возвращать новые объекты, как это делает заглушка
в памяти.

**Конкурентность.** Решатель уже сериализует команды к одному устройству
внутри процесса. На уровне базы это понадобится, только если серверов
станет несколько.

**Время.** Код сейчас работает с наивными датами (`datetime.now()`), без
часового пояса. Предлагаю договориться: хранить в UTC, а перевод делать в
адаптере. Иначе сравнение времён при разных часовых поясах даст ошибки.

**Политика хранения.** История и журнал растут непрерывно. Разумно
удалять записи старше 90 дней, а просроченные приглашения и отозванные
устройства — сразу. Иначе через полгода выборка истории начнёт тормозить.

**Начальное заполнение.** При первом запуске на пустой базе: пять комнат и
двадцать устройств из `app/house.yaml`, плюс первый администратор из
настроек. Повторный запуск не должен создавать дубликаты.

## Чего делать не нужно

**Типы устройств с описанием операций.** Они в коде (`app/domain.py`):
какой параметр регулируется, направление, диапазоны, длительность
блокировки, снимается ли ошибка сама. Это логика проверок, а не данные.
Максимум — таблица `device_types(name, title)` для отображения и ссылочной
целостности.

**Короткие пропуска.** Живут полчаса, создаются на каждый запрос
обновления. Хранить их в базе — лишняя нагрузка без пользы.

**Содержимое заглушки модели.** Это имитация оборудования, а не данные
дома.

## Как проверить

В проекте 191 тест. Они работают на заглушке в памяти, но написаны против
интерфейса, а не против конкретной реализации. Твой слой можно проверить
так: подменить хранилище и прогнать весь набор. Если тесты зелёные —
интерфейс соблюдён.

Отдельно стоит проверить три сценария:
- перезапуск: блокировка и целевое значение на месте, ожидающие команды
  помечены неподтверждёнными;
- повтор события: вторая такая же запись не появляется;
- два одновременных запроса к одному устройству: одна команда уходит
  модели, вторая получает «не требуется».

## Что нужно решить вместе

1. **СУБД.** В проекте предполагалась SQLite. Схема выше под него. Если
   PostgreSQL — скажи, часть решений упростится.
2. **Кто пишет слой доступа.** Могу сделать адаптер сам, если ты дашь
   схему, — тогда ты проверяешь. Или ты делаешь под интерфейс, я проверяю
   тестами.
3. **Миграции.** Нужен способ накатывать схему на чистой базе одной
   командой: для защиты это важно, чтобы не настраивать вручную.
4. **Хранение `state`.** JSON — моё предложение. Если есть причина сделать
   нормализованно, обсудим: чтение устройства станет дороже, но запросы
   вида «все устройства, где включено» — проще. Сейчас таких запросов нет.
