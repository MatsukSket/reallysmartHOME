"""Проверки внутренней шины событий.

Шина заменяет прямые вызовы между модулями: событие публикуется, а
заинтересованные подписчики на него реагируют. Новый подписчик, например
автоматизация нового типа, добавляется без правки ядра.
"""

import pytest

from app import bus
from app.bus import EventBus


def test_subscriber_receives_event():
    """Подписчик получает опубликованное событие."""
    received = []
    events = EventBus()
    events.subscribe(bus.STATE_CHANGED, lambda topic, payload: received.append(payload))

    events.publish(bus.STATE_CHANGED, {"device_id": "light_kitchen"})

    assert received == [{"device_id": "light_kitchen"}]


def test_subscription_to_other_topic_is_not_called():
    """Подписчик не получает события других видов."""
    received = []
    events = EventBus()
    events.subscribe(bus.DEVICE_ERROR, lambda topic, payload: received.append(topic))

    events.publish(bus.STATE_CHANGED, {})

    assert received == []


def test_unsubscribe_stops_delivery():
    """После отписки события больше не приходят."""
    received = []
    events = EventBus()
    unsubscribe = events.subscribe(
        bus.READING_RECEIVED, lambda topic, payload: received.append(payload)
    )

    events.publish(bus.READING_RECEIVED, {"value": 1})
    unsubscribe()
    events.publish(bus.READING_RECEIVED, {"value": 2})

    assert received == [{"value": 1}]


def test_subscribe_all_receives_everything():
    """Подписка на все события получает каждое."""
    received = []
    events = EventBus()
    events.subscribe_all(lambda topic, payload: received.append(topic))

    events.publish(bus.STATE_CHANGED, {})
    events.publish(bus.DEVICE_ERROR, {})

    assert received == [bus.STATE_CHANGED, bus.DEVICE_ERROR]


def test_broken_subscriber_does_not_stop_others():
    """Сбой одного подписчика не мешает остальным."""
    received = []
    events = EventBus()

    def broken(topic, payload):
        raise RuntimeError("сбой")

    events.subscribe(bus.STATE_CHANGED, broken)
    events.subscribe(bus.STATE_CHANGED, lambda topic, payload: received.append(topic))

    events.publish(bus.STATE_CHANGED, {})

    assert received == [bus.STATE_CHANGED]


def test_unknown_topic_is_rejected():
    """Публикация неизвестного события — ошибка программиста."""
    events = EventBus()

    with pytest.raises(KeyError):
        events.publish("нет.такого.события", {})


def test_recursive_publishing_is_limited():
    """Вложенная публикация ограничена по глубине, а не уходит в рекурсию."""
    events = EventBus()
    calls = []

    def handler(topic, payload):
        calls.append(topic)
        events.publish(bus.STATE_CHANGED, {})

    events.subscribe(bus.STATE_CHANGED, handler)
    events.publish(bus.STATE_CHANGED, {})

    assert len(calls) <= events.max_depth


def test_reading_reaches_automation_through_bus(rig):
    """Показание датчика доходит до автоматизации через шину."""
    rig.solver.set_target("bedroom", "temperature", 25.0, "heater_bedroom")
    rig.model.emit(
        __import__("app.domain", fromlist=["Event"]).Event(
            device_id="sensor_temperature_bedroom", kind="reading", value=18.0
        )
    )

    rig.solver.process_events()

    assert rig.state("heater_bedroom")["power"] is True


def test_new_subscriber_sees_state_change(rig):
    """Новый подписчик получает изменения состояния без правки ядра."""
    received = []
    rig.solver.bus.subscribe(
        bus.STATE_CHANGED, lambda topic, payload: received.append(payload)
    )

    rig.solver.execute(
        __import__("app.domain", fromlist=["Command"]).Command(
            device_id="light_kitchen", operation="turn_on"
        )
    )

    assert any(item["device_id"] == "light_kitchen" for item in received)