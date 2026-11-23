"""Фабрика адаптеров. Выбор транспорта — одна настройка GATEWAY_ADAPTER."""
from __future__ import annotations

from .base import DeviceAdapter


def create_adapter(name: str, registry, emit, settings: dict) -> DeviceAdapter:
    common = dict(ack_timeout=settings["ack_timeout"])
    if name == "godot":
        from .godot import GodotAdapter
        return GodotAdapter(registry, emit, host=settings["godot_host"], port=settings["godot_port"], **common)
    if name == "zigbee2mqtt":
        from .zigbee2mqtt import Zigbee2MqttAdapter       # aiomqtt импортируется лениво
        return Zigbee2MqttAdapter(registry, emit, host=settings["mqtt_host"], port=settings["mqtt_port"],
                                  base_topic=settings["mqtt_base_topic"],
                                  username=settings["mqtt_user"], password=settings["mqtt_password"], **common)
    raise ValueError(f"неизвестный адаптер: {name!r} (доступны: godot, zigbee2mqtt)")
