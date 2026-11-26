Для демонстрации с решателем запускайте [проект из корня](../README.md). Инструкции ниже относятся к отдельному шлюзу.

# IoT Gateway (модуль сопряжения)

Шлюз между FastAPI-бэкендом и устройствами. Бэкенд видит только **канонический JSON**; что за ним — симуляция Godot
или реальный Zigbee — определяет адаптер (`GATEWAY_ADAPTER`).

```
Backend ──POST /commands──►  app.py ─► Registry.validate ─► adapter.execute ─► [Godot WS | Zigbee2MQTT]
Backend ◄──POST events────  BackendForwarder ◄─ EventPipeline(валидация, дедуп) ◄─ adapter.emit
```

## Запуск с визуальной панелью (демо без Godot)
Из корня проекта (там, где лежит `pytest.ini`), в **двух** терминалах:
```bash
# 1. шлюз + панель (в демо шлюз сам принимает свои события как «бэкенд»)
DEMO_UI=1 BACKEND_EVENTS_URL=http://127.0.0.1:9000/api/gateway/events \
  python -m uvicorn gateway.app:app --port 9000

# 2. эмулятор Godot: e + Enter = ошибка E04 на котле, l + Enter = протечка
python tools/fake_godot.py
```
Затем откройте в браузере http://localhost:9000/ui

На панели: карточки устройств с живой телеметрией, журнал событий и квитанций, форма команд.
Попробуйте `target_temp = 500` — шлюз ответит `rejected`; остановите эмулятор и отправьте команду — `unavailable`.

При общем запуске `BACKEND_EVENTS_URL` указывает на FastAPI, сервисы используют общий `GATEWAY_TOKEN`, а панель показывает диагностику. Для автономной проверки без служебного токена на панели доступны формы команд.
Тесты: `pytest` (из корня проекта).

## Контракт с бэкендом
* Шлюз → бэкенд: `POST BACKEND_EVENTS_URL`, тело `DeviceEvent`
  `{"event_id","device_id":123,"kind":"telemetry|state|error|discovery|removal","ts","payload":{...},"source":"godot"}`
  (временные сбои приводят к повторной доставке критичных событий).
* Бэкенд → шлюз: `POST /commands` `{"commands":[{"device_id":123,"command":"set_state","params":{"power":"on","target_temp":60}}]}`
  → `{"results":[{"cmd_id","device_id","status":"confirmed|rejected|timeout|unavailable|failed","detail"}]}`

## Как добавить новый транспорт
Наследовать `DeviceAdapter` (`gateway/adapters/base.py`): `start/stop/connected/_dispatch` + вызывать `self.emit(DeviceEvent)`
при входящих данных и `self._acks.resolve(...)` при подтверждении. Зарегистрировать в `adapters/__init__.py`.
Устройствам в `config/devices.json` добавить адрес в `native.<имя_адаптера>`.
