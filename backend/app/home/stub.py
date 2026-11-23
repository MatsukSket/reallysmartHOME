"""Заглушка модели умного дома.

Принимает команды и присылает события так же, как это делала бы модель
Егора. Дополнительно медленно меняет температуру и влажность в
помещениях, чтобы на демонстрации было видно, как решатель сам
поддерживает целевое значение.

Режимы переключаются из теста или через административный запрос:

normal       отвечает как положено;
silence      принимает команду, но событие не присылает;
error        вместо подтверждения присылает ошибку устройства;
garbage      присылает событие с неизвестным устройством или значением;
duplicate    присылает подтверждение дважды;
unavailable  не принимает команды вообще.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from ..domain import Command, Device, Event, is_sensor

OUTDOOR_TEMPERATURE = 16.0
BASE_HUMIDITY = 40.0

# Скорость изменения параметров за секунду модельного времени.
# Подобрана так, чтобы на демонстрации изменения были заметны, но не
# происходили мгновенно: обогреватель поднимает температуру примерно на
# 0,1 °C в минуту.
ROOM_DRIFT = 0.0002
HEATER_RATE = 0.0025
CONDITIONER_RATE = 0.0025
HUMIDIFIER_RATE = 0.008

MODES = ("normal", "silence", "error", "garbage", "duplicate", "unavailable")


class HomeModelUnavailable(RuntimeError):
    """Модель умного дома недоступна."""


class StubHomeModel:
    """Программная модель дома с управляемым поведением."""

    def __init__(
        self, devices: List[Device], mode: str = "normal", clock: Any = None
    ) -> None:
        self.mode = mode
        self.clock = clock
        self._devices: Dict[str, Device] = {device.id: device for device in devices}
        self._events: List[Event] = []
        self._counter = 0
        self._last_tick: Optional[datetime] = None
        self._applied: Dict[str, Dict] = {}
        self._cleaning_until: Dict[str, datetime] = {}
        # Симуляция начинается с текущего момента, иначе первый перевод
        # времени только запоминал бы точку отсчёта и не давал показаний.
        if clock is not None:
            self._last_tick = clock.now()

    def _time(self) -> Optional[datetime]:
        """Возвращает время модели: событие всегда имеет свою метку."""
        if self.clock is not None:
            return self.clock.now()
        return None

    # ------------------------------------------------------------------
    # Управление режимом и состоянием заглушки.
    # ------------------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        """Переключает режим работы заглушки."""
        if mode not in MODES:
            raise ValueError("неизвестный режим модели: " + mode)
        self.mode = mode

    def emit(self, event: Event) -> None:
        """Добавляет событие в очередь вручную, например из теста."""
        self._events.append(event)

    def trigger_reading(
        self, device_id: str, value: float, moment: Optional[datetime] = None
    ) -> None:
        """Присылает показание датчика."""
        self._events.append(
            Event(device_id=device_id, kind="reading", value=value, time=moment)
        )

    def trigger_error(
        self, device_id: str, code: str, moment: Optional[datetime] = None
    ) -> None:
        """Присылает ошибку устройства."""
        self._events.append(
            Event(device_id=device_id, kind="error", code=code, time=moment)
        )

    # ------------------------------------------------------------------
    # Порт модели.
    # ------------------------------------------------------------------

    def send_command(self, command: Command) -> str:
        """Принимает команду и возвращает её номер."""
        if self.mode == "unavailable":
            raise HomeModelUnavailable("модель умного дома недоступна")

        self._counter += 1
        command_id = "cmd-{}".format(self._counter)
        device = self._devices.get(command.device_id)
        if device is None:
            raise KeyError("неизвестное устройство: " + command.device_id)

        spec = device.spec.operation(command.operation)
        if spec is None:
            raise KeyError("операция не поддерживается: " + command.operation)

        changes = spec.desired(command.params)
        device.state.update(changes)
        if command.operation == "reset_error":
            device.error = None
        self._applied[command_id] = changes

        if command.operation == "start_cleaning":
            device.state["place"] = command.params.get("room_id")

        if self.mode == "silence":
            return command_id

        if self.mode == "error":
            self._events.append(
                Event(
                    device_id=device.id,
                    kind="error",
                    code="E10",
                    time=self._time(),
                    command_id=command_id,
                )
            )
            return command_id

        event = Event(
            device_id=device.id,
            kind="state",
            value=dict(changes),
            time=self._time(),
            command_id=command_id,
        )
        self._events.append(event)
        if self.mode == "duplicate":
            self._events.append(
                Event(
                    device_id=device.id,
                    kind="state",
                    value=dict(changes),
                    time=self._time(),
                    command_id=command_id,
                )
            )
        if self.mode == "garbage":
            self._events.append(
                Event(device_id="unknown-device", kind="state", value={"power": True})
            )
            self._events.append(
                Event(
                    device_id=device.id,
                    kind="reading",
                    value=999.0,
                    time=self._time(),
                )
            )
        return command_id

    def drain_events(self) -> List[Event]:
        """Возвращает накопившиеся события и очищает очередь."""
        events = self._events
        self._events = []
        return events

    # ------------------------------------------------------------------
    # Симуляция параметров.
    # ------------------------------------------------------------------

    def tick(self, now: datetime) -> None:
        """Продвигает симуляцию и присылает показания датчиков."""
        self.advance(now)

    def advance(self, now: datetime) -> None:
        """Пересчитывает параметры за прошедшее время и формирует показания."""
        if self._last_tick is None:
            self._last_tick = now
            return
        elapsed = (now - self._last_tick).total_seconds()
        if elapsed <= 0:
            return
        self._last_tick = now

        for device in self._devices.values():
            if device.type == "sensor_temperature":
                device.state["value"] = self._next_temperature(device, elapsed)
                self._events.append(
                    Event(
                        device_id=device.id,
                        kind="reading",
                        value=round(device.state["value"], 2),
                        time=now,
                    )
                )
            elif device.type == "sensor_humidity":
                device.state["value"] = self._next_humidity(device, elapsed)
                self._events.append(
                    Event(
                        device_id=device.id,
                        kind="reading",
                        value=round(device.state["value"], 1),
                        time=now,
                    )
                )

        for device in self._devices.values():
            if device.type == "vacuum" and device.state.get("mode") == "cleaning":
                until = self._cleaning_until.get(device.id)
                if until is not None and now >= until:
                    device.state["mode"] = "docked"
                    device.state["place"] = None
                    self._events.append(
                        Event(device_id=device.id, kind="cleaning_finished", time=now)
                    )
                else:
                    charge = max(0, device.state.get("charge", 100) - 0.05 * elapsed)
                    device.state["charge"] = round(charge, 1)

    def schedule_cleaning_end(self, device_id: str, moment: datetime) -> None:
        """Задаёт момент окончания уборки, чтобы проверить отдельное событие."""
        self._cleaning_until[device_id] = moment

    def _next_temperature(self, sensor: Device, elapsed: float) -> float:
        value = float(sensor.state.get("value", 21.0))
        room = sensor.room_id
        heater = any(
            device.room_id == room
            and device.type in ("heater", "boiler")
            and device.state.get("power")
            for device in self._devices.values()
        )
        conditioner = any(
            device.room_id == room
            and device.type == "conditioner"
            and device.state.get("power")
            for device in self._devices.values()
        )
        rate = ROOM_DRIFT * (OUTDOOR_TEMPERATURE - value)
        if heater:
            rate += HEATER_RATE
        if conditioner:
            rate -= CONDITIONER_RATE
        return value + rate * elapsed

    def _next_humidity(self, sensor: Device, elapsed: float) -> float:
        value = float(sensor.state.get("value", BASE_HUMIDITY))
        room = sensor.room_id
        humidifier = any(
            device.room_id == room
            and device.type == "humidifier"
            and device.state.get("power")
            for device in self._devices.values()
        )
        rate = 0.0005 * (BASE_HUMIDITY - value)
        if humidifier:
            rate += HUMIDIFIER_RATE
        return max(0.0, min(100.0, value + rate * elapsed))

    # ------------------------------------------------------------------
    # Защита от протечек и пылесос.
    # ------------------------------------------------------------------

    def set_leak(self, sensor_id: str, wet: bool, moment: Optional[datetime] = None) -> None:
        """Меняет показание датчика протечки и, при протечке, перекрывает воду."""
        sensor = self._devices[sensor_id]
        sensor.state["value"] = wet
        self._events.append(
            Event(device_id=sensor_id, kind="reading", value=wet, time=moment)
        )
        if not wet:
            return
        for device in self._devices.values():
            if device.type == "leak_protection":
                device.state["water"] = "closed"
                device.state["alarm"] = True
                device.state["place"] = sensor.room_id
                self._events.append(
                    Event(device_id=device.id, kind="state", value=dict(device.state))
                )
                self._events.append(
                    Event(device_id=device.id, kind="error", code="leak")
                )

    def vacuum_charge(self, device_id: str, value: float) -> None:
        """Устанавливает заряд пылесоса."""
        self._devices[device_id].state["charge"] = value

    def device_state(self, device_id: str) -> Dict:
        """Возвращает состояние устройства в модели, для проверок в тестах."""
        return dict(self._devices[device_id].state)

    def has_sensor(self, device_type: str) -> bool:
        """Проверяет, есть ли в модели датчик указанного типа."""
        return any(device.type == device_type for device in self._devices.values())

    def sensor_ids(self, device_type: str) -> List[str]:
        """Возвращает идентификаторы датчиков указанного типа."""
        return [
            device.id
            for device in self._devices.values()
            if device.type == device_type and is_sensor(device.type)
        ]