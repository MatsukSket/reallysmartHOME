"""Проверки журнала и канала живых обновлений."""

from datetime import datetime

from app.domain import Command, Event
from app.hub import ClientHub
from app.journal import Journal


def test_command_is_written_to_journal(rig):
    """Выполненная команда попадает в журнал с итогом."""
    rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    records = rig.solver.journal_records()

    assert any(
        item["subject"] == "light_kitchen" and item["status"] == "выполнено"
        for item in records
    )


def test_rejected_command_is_written_with_reason(rig):
    """Отклонённая команда попадает в журнал вместе с причиной."""
    rig.solver.execute(Command(device_id="no-such-device", operation="turn_on"))

    records = rig.solver.journal_records()

    entry = [item for item in records if item["subject"] == "no-such-device"]
    assert entry
    assert entry[-1]["reason"] == "устройство не найдено"


def test_journal_records_have_impact_id(rig):
    """Каждая запись связана с идентификатором воздействия."""
    rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    for record in rig.solver.journal_records():
        assert record["impact_id"]
        assert record["impact_id"].startswith("imp-")


def test_impact_id_links_whole_chain(rig):
    """Все записи одной обработки находятся по идентификатору воздействия."""
    rig.solver.execute(
        Command(
            device_id="light_kitchen",
            operation="turn_on",
            impact_id="imp-demo",
        )
    )

    records = rig.solver.journal_records(impact_id="imp-demo")

    assert records
    assert {item["impact_id"] for item in records} == {"imp-demo"}


def test_lock_is_written_to_journal(rig):
    """Постановка блокировки видна в журнале."""
    rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    records = rig.solver.journal_records()

    assert any("блокировка" in item["action"] for item in records)


def test_release_lock_is_written_to_journal(rig):
    """Снятие блокировки кнопкой видно в журнале."""
    rig.solver.execute(Command(device_id="heater_bedroom", operation="turn_on"))
    rig.solver.release_lock("heater_bedroom")

    records = rig.solver.journal_records()

    assert any(item["action"] == "снята блокировка" for item in records)


def test_target_change_is_written_to_journal(rig):
    """Изменение целевого значения видно в журнале."""
    rig.solver.set_target("bedroom", "temperature", 22.0, "heater_bedroom")

    records = rig.solver.journal_records()

    assert any(item["action"] == "задано целевое значение" for item in records)


def test_journal_limit_is_respected():
    """Журнал ограничен по числу записей, чтобы не расти бесконечно."""
    journal = Journal(limit=5)
    for index in range(10):
        journal.write(
            impact_id="imp-" + str(index),
            module="тест",
            action="запись",
            at=datetime(2026, 1, 1),
        )

    assert len(journal.records(limit=100)) == 5


def test_client_hub_delivers_to_subscribers():
    """Канал живых обновлений раздаёт событие подключённому клиенту."""
    hub = ClientHub()
    received = []
    hub.subscribe(received.append)

    hub.publish("state.changed", {"device_id": "light_kitchen"})

    assert received == [
        {"topic": "state.changed", "payload": {"device_id": "light_kitchen"}}
    ]


def test_client_hub_keeps_recent_events():
    """Канал помнит последние события: клиент догоняет при подключении."""
    hub = ClientHub()
    for index in range(3):
        hub.publish("state.changed", {"value": index})

    recent = hub.recent(2)

    assert [item["payload"]["value"] for item in recent] == [1, 2]


def test_client_hub_survives_broken_subscriber():
    """Отключившийся клиент не ломает раздачу остальным."""
    hub = ClientHub()
    received = []

    def broken(message):
        raise ConnectionError("клиент отключился")

    hub.subscribe(broken)
    hub.subscribe(received.append)
    hub.publish("state.changed", {"value": 1})

    assert len(received) == 1


def test_live_update_on_state_change(rig):
    """Изменение состояния уходит в канал живых обновлений."""
    from app.bootstrap import attach_live_updates

    hub = ClientHub()
    received = []
    hub.subscribe(received.append)
    attach_live_updates(rig.solver, hub)

    rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    topics = {item["topic"] for item in received}
    assert "state.changed" in topics or "command.finished" in topics


def test_live_update_on_event(rig):
    """Событие модели уходит в канал живых обновлений."""
    from app.bootstrap import attach_live_updates

    hub = ClientHub()
    received = []
    hub.subscribe(received.append)
    attach_live_updates(rig.solver, hub)

    rig.solver.accept_event(
        Event(device_id="sensor_temperature_bedroom", kind="reading", value=22.0)
    )

    topics = {item["topic"] for item in received}
    assert "reading.received" in topics


def test_minute_event_is_not_sent_to_clients(rig):
    """Служебное событие «минута прошла» не уходит клиентам."""
    from app.bootstrap import attach_live_updates

    hub = ClientHub()
    received = []
    hub.subscribe(received.append)
    attach_live_updates(rig.solver, hub)

    rig.solver.run_schedules()

    topics = {item["topic"] for item in received}
    assert "minute.passed" not in topics