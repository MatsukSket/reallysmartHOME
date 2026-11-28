"""Модель предметной области решателя.

Здесь описаны типы устройств, их операции, состояния, целевые значения,
расписания и результаты выполнения команд. Никакой логики обработки нет:
только данные и правила проверки, которые используют модули решателя.
"""

from math import isfinite
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Dict, List, Optional

import threading

# ---------------------------------------------------------------------------
# Статусы результата выполнения команды.
# ---------------------------------------------------------------------------

STATUS_DONE = "выполнено"
STATUS_NOT_REQUIRED = "не требуется"
STATUS_REJECTED = "отклонено"
STATUS_UNCONFIRMED = "не подтверждено"

# ---------------------------------------------------------------------------
# Источники команд и их приоритеты. Чем меньше число, тем выше приоритет.
# ---------------------------------------------------------------------------

SOURCE_MANUAL = "manual"
SOURCE_ASSISTANT = "assistant"
SOURCE_TARGET = "target"
SOURCE_SCHEDULE = "schedule"

PRIORITY = {
    SOURCE_MANUAL: 1,
    SOURCE_ASSISTANT: 1,
    SOURCE_TARGET: 2,
    SOURCE_SCHEDULE: 3,
}

# Источники, после которых ставится блокировка.
LOCKING_SOURCES = (SOURCE_MANUAL, SOURCE_ASSISTANT)

# ---------------------------------------------------------------------------
# Параметры, которые регулируют исполнительные устройства.
# ---------------------------------------------------------------------------

PARAM_TEMPERATURE = "temperature"
PARAM_HUMIDITY = "humidity"


def opposite_parameter(parameter: str) -> str:
    """Возвращает параметр, противоположный температуре: влажность.

    Функция нужна, чтобы не дублировать проверки для двух параметров.
    """
    return PARAM_HUMIDITY if parameter == PARAM_TEMPERATURE else PARAM_TEMPERATURE


# ---------------------------------------------------------------------------
# Описание операции устройства.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationSpec:
    """Операция, поддерживаемая устройством.

    property --- имя свойства состояния, которое меняет операция.
    fixed_value --- значение свойства для операций без параметра.
    param_property --- свойство, куда записывается значение параметра.
    min_value и max_value --- допустимый диапазон параметра.
    requires_error --- операция допустима только при наличии ошибки.
    """

    name: str
    property: str
    fixed_value: Any = None
    param_property: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    requires_error: bool = False
    choices: tuple[str, ...] = ()

    def desired(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Возвращает состояние, которого требует операция."""
        if self.param_property is not None:
            value = params.get("value")
            return {self.param_property: value}
        return {self.property: self.fixed_value}

    def validate(self, params: Dict[str, Any]) -> Optional[str]:
        """Проверяет параметры операции, возвращает причину отказа или None."""
        if self.param_property is None:
            return None
        if "value" not in params:
            return "не указано значение параметра"
        value = params["value"]
        if self.choices:
            return None if value in self.choices else "недопустимое значение параметра"
        if not isinstance(value, (int, float)) or isinstance(value, bool) or (isinstance(value, float) and not isfinite(value)):
            return "значение параметра должно быть числом"
        if self.min_value is not None and value < self.min_value:
            return "значение меньше допустимого"
        if self.max_value is not None and value > self.max_value:
            return "значение больше допустимого"
        return None


# ---------------------------------------------------------------------------
# Описание типа устройства.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceType:
    """Тип устройства и его возможности.

    regulates --- параметр, который регулирует устройство, либо None.
    direction --- направление регулирования: +1 увеличивает параметр,
    -1 уменьшает.
    lock_ttl --- длительность блокировки после ручной команды, секунд.
    error_requires_reset --- ошибка снимается только командой сброса.
    state_ranges --- допустимые диапазоны значений состояния.
    """

    name: str
    operations: Dict[str, OperationSpec]
    regulates: Optional[str] = None
    direction: int = 0
    lock_ttl: int = 2 * 60 * 60
    error_requires_reset: bool = False
    state_ranges: Dict[str, tuple] = field(default_factory=dict)

    def operation(self, name: str) -> Optional[OperationSpec]:
        """Возвращает описание операции или None, если она не поддерживается."""
        return self.operations.get(name)


def _on_off(prefix: str) -> Dict[str, OperationSpec]:
    """Возвращает пару операций включения и выключения."""
    return {
        "turn_on": OperationSpec(name="turn_on", property="power", fixed_value=True),
        "turn_off": OperationSpec(name="turn_off", property="power", fixed_value=False),
    }


def _specs() -> Dict[str, DeviceType]:
    """Собирает описание всех типов устройств дома."""
    result: Dict[str, DeviceType] = {}

    result["light"] = DeviceType(
        name="light",
        operations={
            **_on_off("light"),
            "set_brightness": OperationSpec(
                name="set_brightness",
                property="brightness",
                param_property="brightness",
                min_value=1,
                max_value=100,
            ),
        },
        lock_ttl=20 * 60,
        state_ranges={"brightness": (1, 100)},
    )

    result["heater"] = DeviceType(
        name="heater",
        operations=_on_off("heater"),
        regulates=PARAM_TEMPERATURE,
        direction=1,
    )

    result["conditioner"] = DeviceType(
        name="conditioner",
        operations={
            **_on_off("conditioner"),
            "set_target_temperature": OperationSpec(
                "set_target_temperature", "target_temp", param_property="target_temp", min_value=16, max_value=30,
            ),
            "set_mode": OperationSpec(
                "set_mode", "mode", param_property="mode", choices=("cool", "heat", "fan", "auto"),
            ),
        },
        state_ranges={"target_temp": (16, 30)},
        regulates=PARAM_TEMPERATURE,
        direction=-1,
    )

    result["boiler"] = DeviceType(
        name="boiler",
        operations={
            **_on_off("boiler"),
            "set_coolant_temperature": OperationSpec(
                name="set_coolant_temperature",
                property="coolant_temperature",
                param_property="coolant_temperature",
                min_value=30,
                max_value=80,
            ),
            "reset_error": OperationSpec(
                name="reset_error",
                property="error",
                fixed_value=None,
                requires_error=True,
            ),
        },
        regulates=PARAM_TEMPERATURE,
        direction=1,
        error_requires_reset=True,
        state_ranges={"coolant_temperature": (30, 80)},
    )

    result["humidifier"] = DeviceType(
        name="humidifier",
        operations=_on_off("humidifier"),
        regulates=PARAM_HUMIDITY,
        direction=1,
    )

    result["socket"] = DeviceType(
        name="socket",
        operations={
            **_on_off("socket"),
            "set_power_limit": OperationSpec(
                name="set_power_limit",
                property="power_limit",
                param_property="power_limit",
                min_value=500,
                max_value=3000,
            ),
        },
        state_ranges={"power_limit": (500, 3000)},
    )

    result["vacuum"] = DeviceType(
        name="vacuum",
        operations={
            "start_cleaning": OperationSpec(
                name="start_cleaning", property="mode", fixed_value="cleaning"
            ),
            "pause": OperationSpec(name="pause", property="mode", fixed_value="paused"),
            "resume": OperationSpec(
                name="resume", property="mode", fixed_value="cleaning"
            ),
            "dock": OperationSpec(name="dock", property="mode", fixed_value="docked"),
        },
        state_ranges={"charge": (0, 100)},
    )

    # Защита от протечек защищает себя сама, поэтому блокировка ей не нужна.
    result["leak_protection"] = DeviceType(
        name="leak_protection",
        operations={
            "open_water": OperationSpec(
                name="open_water", property="water", fixed_value="open"
            ),
            "close_water": OperationSpec(
                name="close_water", property="water", fixed_value="closed"
            ),
        },
        lock_ttl=0,
    )

    # Датчики не принимают команд: список операций пуст.
    for sensor in ("temperature", "humidity", "motion", "leak"):
        result["sensor_" + sensor] = DeviceType(name="sensor_" + sensor, operations={})

    return result


DEVICE_TYPES: Dict[str, DeviceType] = _specs()

# Допустимые диапазоны показаний датчиков.
SENSOR_RANGES = {
    "sensor_temperature": (0.0, 50.0),
    "sensor_humidity": (0.0, 100.0),
}

SENSOR_PARAMETER = {
    "sensor_temperature": PARAM_TEMPERATURE,
    "sensor_humidity": PARAM_HUMIDITY,
}


def is_sensor(device_type: str) -> bool:
    """Проверяет, является ли тип устройства датчиком."""
    return device_type.startswith("sensor_")


# ---------------------------------------------------------------------------
# Состояние устройства.
# ---------------------------------------------------------------------------


@dataclass
class Room:
    """Помещение дома."""

    id: str
    name: str


@dataclass
class Device:
    """Устройство дома и его текущее состояние."""

    id: str
    name: str
    type: str
    room_id: str
    state: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    # Время события, последним изменившего состояние.
    last_event_time: Optional[datetime] = None
    # Время получения этого события решателем: по нему считается неактуальность.
    received_at: Optional[datetime] = None
    connected: bool = True
    retired: bool = False
    model: str = ""

    @property
    def spec(self) -> DeviceType:
        """Возвращает описание типа устройства."""
        return DEVICE_TYPES[self.type]

    def is_stale(self, now: datetime, stale_after: int) -> bool:
        """Проверяет, устарели ли сведения о состоянии устройства."""
        if self.received_at is None:
            return True
        return (now - self.received_at).total_seconds() > stale_after

    def value(self, property_name: str) -> Any:
        """Возвращает значение свойства состояния."""
        return self.state.get(property_name)


# ---------------------------------------------------------------------------
# Определение противоречия команды автоматики блокировке.
# ---------------------------------------------------------------------------


@dataclass
class Lock:
    """Блокировка устройства после ручной команды.

    property_name и value описывают, что именно изменил пользователь.
    Противоречие --- это команда автоматики, которая меняет то же свойство
    на другое значение.
    """

    device_id: str
    property_name: str
    value: Any
    until: datetime

    def is_active(self, now: datetime) -> bool:
        """Проверяет, действует ли блокировка в указанный момент."""
        return now < self.until

    def contradicts(self, property_name: str, value: Any) -> bool:
        """Проверяет, противоречит ли команда блокировке."""
        if property_name != self.property_name:
            return False
        return value != self.value


# ---------------------------------------------------------------------------
# Целевое значение параметра.
# ---------------------------------------------------------------------------


@dataclass
class Target:
    """Целевое значение параметра в помещении.

    value --- действующее значение, device_id --- регулирующее устройство.
    base_value --- значение, к которому возвращает расписание целевого
    значения; None означает, что после расписания регулирующее устройство
    выключается.
    """

    room_id: str
    parameter: str
    value: float
    device_id: str
    base_value: Optional[float] = None
    base_device_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Расписания.
# ---------------------------------------------------------------------------

SCHEDULE_COMMAND = "command"
SCHEDULE_TARGET = "target"


@dataclass
class ScheduleMoment:
    """Один момент расписания «время -> действие».

    Для вида command заданы device_id, operation и params.
    Для вида target заданы room_id, parameter и value.
    next_due --- ближайшее время срабатывания, вычисленное при создании.
    """

    id: str
    kind: str
    at_time: time
    days: List[int]
    device_id: Optional[str] = None
    operation: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    room_id: Optional[str] = None
    parameter: Optional[str] = None
    value: Optional[float] = None
    next_due: Optional[datetime] = None
    source_pair: Optional[str] = None


@dataclass
class SchedulePair:
    """Расписание целевого значения, заданное парой моментов.

    Пользователь указывает, к какому времени параметр должен принять
    заданное значение и до какого времени его удерживать. Решатель сам
    вычитает упреждение и создаёт два момента. base_value --- значение,
    к которому возвращает второй момент.
    """

    id: str
    room_id: str
    parameter: str
    value: float
    at_time: time
    until_time: time
    days: List[int]
    base_value: Optional[float] = None


# ---------------------------------------------------------------------------
# Команда и её результат.
# ---------------------------------------------------------------------------


@dataclass
class Command:
    """Управляющая команда, поступающая в единый путь выполнения.

    impact_id связывает все записи журнала, относящиеся к одной обработке
    запроса пользователя. Для внутренних команд он создаётся решателем.
    """

    device_id: str
    operation: str
    params: Dict[str, Any] = field(default_factory=dict)
    source: str = SOURCE_MANUAL
    actor: str = "пользователь"
    impact_id: Optional[str] = None


@dataclass
class CommandResult:
    """Результат проверки и выполнения команды."""

    status: str
    reason: Optional[str] = None
    model_command_id: Optional[str] = None
    property_name: Optional[str] = None
    state: Optional[Dict[str, Any]] = None


@dataclass
class Event:
    """Событие модели умного дома.

    kind: reading --- показание датчика, state --- смена состояния,
    error --- ошибка устройства, cleaning_finished --- уборка завершена.
    """

    device_id: str
    kind: str
    value: Any = None
    time: Optional[datetime] = None
    code: Optional[str] = None
    command_id: Optional[str] = None
    event_id: Optional[str] = None
    measurements: Dict[str, Any] = field(default_factory=dict)
    fault_reported: bool = False


@dataclass
class Pending:
    """Команда, отправленная модели и ожидающая подтверждения.

    expected --- состояние, которого ждёт решатель. deadline --- момент, до
    которого подтверждение ещё принимается. wake --- сигнал досрочного
    пробуждения ожидающего. impact_id связывает команду с записью журнала.
    """

    model_id: str
    device_id: str
    expected: Dict[str, Any]
    deadline: datetime
    source: str
    operation: str = ""
    wake: Any = field(default_factory=threading.Event)
    impact_id: Optional[str] = None
    actor: Optional[str] = None
