"""Адаптер реальных Zigbee-устройств через Zigbee2MQTT (координатор + брокер MQTT).
Сам Zigbee — не JSON: JSON-представление устройств даёт Zigbee2MQTT:
  zigbee2mqtt/<friendly_name>              -> {"state":"ON","temperature":21.5,...}   (состояние)
  zigbee2mqtt/<friendly_name>/set          <- {"state":"OFF"}                         (команда)
  zigbee2mqtt/<friendly_name>/availability -> {"state":"online"|"offline"}

У Zigbee нет квитанции с cmd_id, поэтому квитирование = ПОДТВЕРЖДЕНИЕ СОСТОЯНИЕМ:
команда считается выполненной, когда устройство прислало состояние с запрошенными значениями.
Нет такого состояния за ack_timeout -> TIMEOUT (ровно та же семантика, что и в симуляции).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..models import Command, CommandStatus, DeviceEvent, EventKind
from ..registry import Device
from .base import DeviceAdapter

log = logging.getLogger("gateway.z2m")

# каноническое имя -> имя атрибута z2m (можно переопределить per-device в registry: attr_map.zigbee2mqtt)
DEFAULT_ATTR = {"power": "state", "target_temp": "current_heating_setpoint"}
NOISE = {"linkquality", "last_seen", "update", "update_available", "voltage", "power_on_behavior"}
ALARM_FLAGS = {"water_leak": "WATER_LEAK", "smoke": "SMOKE", "tamper": "TAMPER",
               "battery_low": "BATTERY_LOW", "gas": "GAS", "vibration": "VIBRATION"}


def _attr_map(dev: Device) -> dict[str, str]:
    return {**DEFAULT_ATTR, **dev.attr_map.get("zigbee2mqtt", {})}


def to_z2m(dev: Device, params: dict[str, Any]) -> dict[str, Any]:
    """Канонические параметры команды -> payload для .../set"""
    amap, out = _attr_map(dev), {}
    for k, v in params.items():
        z = amap.get(k, k)
        out[z] = {"on": "ON", "off": "OFF"}.get(v, v) if z == "state" else v
    return out


def from_z2m(dev: Device, payload: dict[str, Any]) -> tuple[dict, dict, list[str]]:
    """Payload состояния z2m -> (state, telemetry, alarms) в канонических именах."""
    inv = {v: k for k, v in _attr_map(dev).items()}
    state, telemetry, alarms = {}, {}, []
    for z, v in payload.items():
        if z in NOISE:
            continue
        if z in ALARM_FLAGS:
            if v is True:
                alarms.append(ALARM_FLAGS[z])
            continue
        k = inv.get(z, z)
        if z == "state" and isinstance(v, str):
            v = v.lower()
        if k in dev.state_keys:
            state[k] = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            telemetry[k] = v
    return state, telemetry, alarms


def _matches(expected: dict, actual: dict) -> bool:
    for k, v in expected.items():
        a = actual.get(k)
        if isinstance(v, (int, float)) and isinstance(a, (int, float)) \
                and not isinstance(v, bool) and not isinstance(a, bool):
            if abs(v - a) > 0.5:                      # допуск округления у термостатов
                return False
        elif a != v:
            return False
    return True


class Zigbee2MqttAdapter(DeviceAdapter):
    name = "zigbee2mqtt"

    def __init__(self, *args: Any, host: str = "localhost", port: int = 1883,
                 base_topic: str = "zigbee2mqtt", username: str | None = None,
                 password: str | None = None, **kw: Any):
        super().__init__(*args, **kw)
        self._cfg = dict(hostname=host, port=port, username=username, password=password)
        self._base = base_topic
        self._client = None
        self._task: asyncio.Task | None = None
        self._expect: dict[str, tuple[int, dict]] = {}   # cmd_id -> (device_id, ожидаемое состояние)

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="z2m-mqtt")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        import aiomqtt
        delay = 1.0
        while True:
            try:
                async with aiomqtt.Client(**self._cfg) as client:
                    self._client, delay = client, 1.0
                    await client.subscribe(f"{self._base}/#")
                    log.info("подключён к MQTT %s:%s", self._cfg["hostname"], self._cfg["port"])
                    async for msg in client.messages:
                        await self._on_message(str(msg.topic), msg.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                                   # noqa: BLE001
                log.warning("MQTT: %s; повтор через %.0f с", exc, delay)
            finally:
                if self._client is not None:
                    self._client = None
                    self._acks.fail_all(CommandStatus.UNAVAILABLE, "соединение с MQTT потеряно")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _on_message(self, topic: str, raw: bytes | bytearray | str) -> None:
        rel = topic.removeprefix(self._base + "/")
        if rel.startswith("bridge/") or rel.endswith("/set") or rel.endswith("/get"):
            return
        is_avail = rel.endswith("/availability")
        name = rel.removesuffix("/availability")
        dev = self.registry.by_native(name)
        if dev is None:
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"state": raw.decode() if isinstance(raw, (bytes, bytearray)) else raw}

        if is_avail:
            if payload.get("state") == "offline":
                await self.emit(DeviceEvent(device_id=dev.id, kind=EventKind.ERROR, source=self.name,
                                            payload={"code": "DEVICE_OFFLINE", "message": "устройство недоступно"}))
            return
        if not isinstance(payload, dict):
            return

        state, telemetry, alarms = from_z2m(dev, payload)
        for code in alarms:
            await self.emit(DeviceEvent(device_id=dev.id, kind=EventKind.ERROR, source=self.name,
                                        payload={"code": code, "message": ""}))
        if telemetry:
            await self.emit(DeviceEvent(device_id=dev.id, kind=EventKind.TELEMETRY,
                                        source=self.name, payload=telemetry))
        if state:
            await self.emit(DeviceEvent(device_id=dev.id, kind=EventKind.STATE,
                                        source=self.name, payload=state))
            for cmd_id, (dev_id, expected) in list(self._expect.items()):   # подтверждение состоянием
                if dev_id == dev.id and _matches(expected, state):
                    self._acks.resolve(cmd_id, CommandStatus.CONFIRMED)

    async def _dispatch(self, cmd: Command, native_addr: str) -> None:
        dev = self.registry.get(cmd.device_id)
        assert dev is not None and self._client is not None
        self._expect[cmd.cmd_id] = (dev.id, dict(cmd.params))
        try:
            await self._client.publish(f"{self._base}/{native_addr}/set",
                                       json.dumps(to_z2m(dev, cmd.params)), qos=1)
        except Exception:
            self._expect.pop(cmd.cmd_id, None)
            raise

    async def execute(self, cmd):                                         # чистим таблицу ожиданий
        try:
            return await super().execute(cmd)
        finally:
            self._expect.pop(cmd.cmd_id, None)
