"""Модуль автоматизации.

Две функции: поддержание целевых значений параметров и выполнение
расписаний. Обе не обращаются к модели дома напрямую, а формируют
команды и передают их в единый путь выполнения.

Расписание --- набор моментов «время -> действие». Интервал задаётся
двумя моментами. Действие бывает двух видов: команда устройству или
изменение целевого значения.

Расписание целевого значения задаётся парой моментов: к какому времени
привести параметр к заданному значению и до какого времени его
удерживать. Оба момента обязательны. Решатель сам вычитает упреждение,
чтобы к нужному времени параметр уже был приведён, и возвращает обычное
целевое значение ко второму моменту.
"""

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..config import (
    SCHEDULE_LEAD_S,
    SCHEDULE_TOLERANCE_S,
    STALE_AFTER_S,
    delta_for,
)
from ..domain import (
    SCHEDULE_COMMAND,
    SCHEDULE_TARGET,
    SOURCE_SCHEDULE,
    SOURCE_TARGET,
    Command,
    Device,
    ScheduleMoment,
    SchedulePair,
    Target,
)

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Solver


class ScheduleError(ValueError):
    """Расписание или целевое значение не может быть создано."""


def minutes_of(moment: time) -> int:
    """Переводит время в минуты от начала суток."""
    return moment.hour * 60 + moment.minute


def shift_minutes(moment: time, minutes: int) -> time:
    """Сдвигает время суток на указанное число минут."""
    total = (minutes_of(moment) + minutes) % (24 * 60)
    return time(total // 60, total % 60)


def lead_minutes() -> int:
    """Возвращает упреждение расписания в минутах."""
    return SCHEDULE_LEAD_S // 60


# ---------------------------------------------------------------------------
# Поддержание целевого значения.
# ---------------------------------------------------------------------------


def on_reading(solver: "Solver", sensor: Device) -> None:
    """Запускает поддержание значения после показания датчика."""
    parameter = measured_parameter(sensor)
    if parameter is None:
        return
    if sensor.is_stale(solver.clock.now(), STALE_AFTER_S):
        return

    value = sensor.state.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return

    maintain(solver, sensor.room_id, parameter, float(value))


def measured_parameter(sensor: Device) -> Optional[str]:
    """Возвращает параметр, который измеряет датчик."""
    if sensor.type == "sensor_temperature":
        return "temperature"
    if sensor.type == "sensor_humidity":
        return "humidity"
    return None


def maintain(
    solver: "Solver", room_id: str, parameter: str, value: float
) -> List[Any]:
    """Поддерживает целевое значение параметра в помещении.

    Устройство, повышающее параметр (обогреватель, котёл, увлажнитель),
    включается при значении ниже цели и выключается при значении выше
    цели. Устройство, понижающее параметр (кондиционер), действует
    наоборот. Внутри зоны нечувствительности команд не требуется.
    """
    results = []
    delta = delta_for(parameter)

    for target in solver.storage.targets(room_id):
        if target.parameter != parameter:
            continue

        device = solver.storage.device(target.device_id)
        if device is None or device.spec.regulates != parameter:
            continue

        operation = choose_operation(
            device.spec.direction, value, target.value, delta
        )
        if operation is None:
            continue

        results.append(
            solver.execute(
                Command(
                    device_id=device.id,
                    operation=operation,
                    source=SOURCE_TARGET,
                    actor="целевое значение",
                )
            )
        )

    return results


def choose_operation(
    direction: int, value: float, target_value: float, delta: float
) -> Optional[str]:
    """Выбирает операцию регулирующего устройства по показанию."""
    if direction > 0:
        if value < target_value - delta:
            return "turn_on"
        if value > target_value + delta:
            return "turn_off"
        return None

    if value > target_value + delta:
        return "turn_on"
    if value < target_value - delta:
        return "turn_off"
    return None


# ---------------------------------------------------------------------------
# Целевые значения.
# ---------------------------------------------------------------------------


def set_target(
    solver: "Solver",
    room_id: str,
    parameter: str,
    value: float,
    device_id: Optional[str] = None,
) -> Target:
    """Задаёт целевое значение и закрепляет регулирующее устройство."""
    if solver.storage.room(room_id) is None:
        raise ScheduleError("помещение не найдено")

    if device_id is not None:
        device = solver.storage.device(device_id)
        if device is None:
            raise ScheduleError("устройство не найдено")
        if device.spec.regulates != parameter:
            raise ScheduleError(
                "устройство не регулирует параметр «{}»".format(parameter)
            )
        if device.room_id != room_id:
            raise ScheduleError("устройство находится в другом помещении")
    else:
        device = pick_regulator(solver, room_id, parameter)
        if device is None:
            raise ScheduleError("в помещении нет устройства, регулирующего параметр")

    if has_command_schedules(solver, device.id):
        raise ScheduleError(
            "у устройства есть расписания команд; сначала удалите их,"
            " затем задайте целевое значение"
        )

    existing = find_target(solver, room_id, parameter)
    if existing is not None and existing.base_value is not None:
        base_value = existing.base_value
        base_device_id = existing.base_device_id or existing.device_id
    elif existing is not None:
        base_value = existing.value
        base_device_id = existing.device_id
    else:
        base_value = value
        base_device_id = device.id

    target = Target(
        room_id=room_id,
        parameter=parameter,
        value=value,
        device_id=device.id,
        base_value=base_value,
        base_device_id=base_device_id,
    )
    solver.storage.save_target(target)
    return target


def clear_target(solver: "Solver", room_id: str, parameter: str) -> bool:
    """Удаляет целевое значение."""
    target = find_target(solver, room_id, parameter)
    if target is None:
        return False
    solver.storage.delete_target(room_id, parameter)
    return True


def find_target(solver: "Solver", room_id: str, parameter: str) -> Optional[Target]:
    """Находит целевое значение для параметра в помещении."""
    for target in solver.storage.targets(room_id):
        if target.parameter == parameter:
            return target
    return None


def pick_regulator(solver: "Solver", room_id: str, parameter: str) -> Optional[Device]:
    """Выбирает устройство, регулирующее параметр в помещении."""
    for device in solver.storage.devices(room_id):
        if device.spec.regulates == parameter:
            return device
    return None


def has_command_schedules(solver: "Solver", device_id: str) -> bool:
    """Проверяет, есть ли у устройства расписания команд."""
    return any(
        moment.kind == SCHEDULE_COMMAND and moment.device_id == device_id
        for moment in solver.schedules
    )


# ---------------------------------------------------------------------------
# Создание расписаний.
# ---------------------------------------------------------------------------


def create_command_schedule(
    solver: "Solver",
    device_id: str,
    operation: str,
    at: time,
    days: Optional[List[int]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> ScheduleMoment:
    """Создаёт расписание команды устройству."""
    device = solver.storage.device(device_id)
    if device is None:
        raise ScheduleError("устройство не найдено")

    spec = device.spec.operation(operation)
    if spec is None:
        raise ScheduleError("операция не поддерживается устройством")
    problem = spec.validate(params or {})
    if problem is not None:
        raise ScheduleError(problem)

    if device.spec.regulates is not None:
        target = find_target(solver, device.room_id, device.spec.regulates)
        if target is not None:
            raise ScheduleError(
                "устройство управляется целевым значением;"
                " задайте расписание целевого значения"
            )

    moment = ScheduleMoment(
        id=next_id(solver, "sch"),
        kind=SCHEDULE_COMMAND,
        at_time=at,
        days=days or list(range(7)),
        device_id=device_id,
        operation=operation,
        params=dict(params or {}),
    )

    check_command_conflict(solver, moment)
    moment.next_due = next_due(moment, solver.clock.now())
    solver.schedules.append(moment)
    return moment


def create_target_schedule(
    solver: "Solver",
    room_id: str,
    parameter: str,
    value: float,
    at: Optional[time],
    until: Optional[time],
    days: Optional[List[int]] = None,
) -> SchedulePair:
    """Создаёт расписание целевого значения по паре моментов."""
    if solver.storage.room(room_id) is None:
        raise ScheduleError("помещение не найдено")

    # Оба момента обязательны: без второго непонятно, когда вернуться
    # к обычному значению.
    if at is None:
        raise ScheduleError("укажите, к какому времени задать значение")
    if until is None:
        raise ScheduleError("укажите, до какого времени удерживать значение")

    hold = minutes_of(until) - minutes_of(at)
    if hold <= 0:
        raise ScheduleError(
            "время окончания должно быть позже времени начала"
            " в пределах суток"
        )
    if hold < lead_minutes():
        raise ScheduleError(
            "время окончания должно быть позже времени начала"
            " не менее чем на {} минут".format(lead_minutes())
        )

    target = find_target(solver, room_id, parameter)
    if target is None:
        raise ScheduleError("сначала задайте целевое значение параметра в помещении")

    regulator = solver.storage.device(target.device_id)
    if regulator is None:
        raise ScheduleError("регулирующее устройство не найдено")
    if has_command_schedules(solver, regulator.id):
        raise ScheduleError(
            "у регулирующего устройства есть расписания команд;"
            " сначала удалите их"
        )

    schedule_days = days or list(range(7))
    pair_id = next_id(solver, "pair")

    # Обычное целевое значение задано --- к нему и возвращаемся. Не задано
    # --- после интервала регулирующее устройство выключается.
    base = target.base_value

    interval = (at, until)
    check_target_conflict(solver, room_id, parameter, interval, schedule_days)

    pair = SchedulePair(
        id=pair_id,
        room_id=room_id,
        parameter=parameter,
        value=value,
        at_time=at,
        until_time=until,
        days=schedule_days,
        base_value=base,
    )
    solver.pairs.append(pair)

    start = shift_minutes(at, -lead_minutes())
    end = shift_minutes(until, -lead_minutes())

    first = ScheduleMoment(
        id=next_id(solver, "sch"),
        kind=SCHEDULE_TARGET,
        at_time=start,
        days=schedule_days,
        room_id=room_id,
        parameter=parameter,
        value=value,
        source_pair=pair_id,
    )
    solver.schedules.append(first)

    second = ScheduleMoment(
        id=next_id(solver, "sch"),
        kind=SCHEDULE_TARGET,
        at_time=end,
        days=schedule_days,
        room_id=room_id,
        parameter=parameter,
        value=base,
        source_pair=pair_id,
    )
    solver.schedules.append(second)

    now = solver.clock.now()
    for moment in (first, second):
        moment.next_due = next_due(moment, now)

    return pair


def check_command_conflict(solver: "Solver", moment: ScheduleMoment) -> None:
    """Проверяет, что расписание не совпадает с другим того же устройства."""
    for existing in solver.schedules:
        if existing.kind != SCHEDULE_COMMAND:
            continue
        if existing.device_id != moment.device_id:
            continue
        if existing.at_time != moment.at_time:
            continue
        if existing.operation == moment.operation and existing.params == moment.params:
            return
        if set(existing.days) & set(moment.days):
            raise ScheduleError(
                "расписание пересекается с другим расписанием этого устройства"
            )


def check_target_conflict(
    solver: "Solver",
    room_id: str,
    parameter: str,
    interval: tuple,
    days: List[int],
) -> None:
    """Проверяет, что интервалы расписаний одного параметра не накладываются."""
    for pair in solver.pairs:
        if pair.room_id != room_id or pair.parameter != parameter:
            continue
        if not set(pair.days) & set(days):
            continue
        if intervals_overlap(interval, (pair.at_time, pair.until_time)):
            raise ScheduleError(
                "расписание пересекается с другим расписанием"
                " этого параметра в помещении"
            )


def intervals_overlap(first: tuple, second: tuple) -> bool:
    """Проверяет, пересекаются ли два интервала времени суток."""
    first = sorted(minutes_of(item) % (24 * 60) for item in first)
    second = sorted(minutes_of(item) % (24 * 60) for item in second)
    return first[0] < second[1] and second[0] < first[1]


def next_id(solver: "Solver", prefix: str) -> str:
    """Возвращает следующий идентификатор расписания."""
    used = {moment.id for moment in solver.schedules}
    used |= {pair.id for pair in solver.pairs}
    index = 1
    while "{}-{}".format(prefix, index) in used:
        index += 1
    return "{}-{}".format(prefix, index)


def delete_schedule(solver: "Solver", schedule_id: str) -> bool:
    """Удаляет расписание и связанную с ним пару моментов."""
    found = [moment for moment in solver.schedules if moment.id == schedule_id]
    if not found:
        return False

    moment = found[0]
    if moment.source_pair is None:
        solver.schedules = [
            item for item in solver.schedules if item.id != moment.id
        ]
        return True

    pair_id = moment.source_pair
    solver.schedules = [
        item for item in solver.schedules if item.source_pair != pair_id
    ]
    solver.pairs = [pair for pair in solver.pairs if pair.id != pair_id]
    return True


# ---------------------------------------------------------------------------
# Выполнение расписаний.
# ---------------------------------------------------------------------------


def next_due(moment: ScheduleMoment, now: datetime) -> datetime:
    """Вычисляет ближайшее время срабатывания расписания."""
    candidate = datetime.combine(now.date(), moment.at_time)
    for offset in range(8):
        current = candidate + timedelta(days=offset)
        if current.weekday() not in moment.days:
            continue
        if current >= now:
            return current
    return candidate + timedelta(days=7)


def run_schedules(solver: "Solver") -> int:
    """Выполняет наступившие расписания и возвращает их число."""
    now = solver.clock.now()
    executed = 0

    for moment in list(solver.schedules):
        if moment.next_due is None:
            moment.next_due = next_due(moment, now)
        if moment.next_due > now:
            continue

        if (now - moment.next_due).total_seconds() <= SCHEDULE_TOLERANCE_S:
            execute_moment(solver, moment)
            executed += 1

        moment.next_due = next_due(moment, now + timedelta(seconds=1))

    return executed


def execute_moment(solver: "Solver", moment: ScheduleMoment) -> Any:
    """Выполняет действие одного момента расписания."""
    from .. import bus

    if moment.kind == SCHEDULE_COMMAND:
        result = solver.execute(
            Command(
                device_id=moment.device_id,
                operation=moment.operation,
                params=dict(moment.params),
                source=SOURCE_SCHEDULE,
                actor="расписание",
            )
        )
        solver._publish(
            bus.SCHEDULE_FIRED,
            {
                "schedule_id": moment.id,
                "device_id": moment.device_id,
                "operation": moment.operation,
                "status": getattr(result, "status", None),
            },
        )
        # Плановое срабатывание снимает блокировку с устройства.
        solver.release_lock(moment.device_id)
        return result

    if moment.kind == SCHEDULE_TARGET:
        result = apply_target_moment(solver, moment)
        solver._publish(
            bus.SCHEDULE_FIRED,
            {
                "schedule_id": moment.id,
                "room_id": moment.room_id,
                "parameter": moment.parameter,
                "value": moment.value,
            },
        )
        return result

    return None


def apply_target_moment(solver: "Solver", moment: ScheduleMoment) -> Any:
    """Применяет момент расписания целевого значения."""
    from .. import bus

    target = find_target(solver, moment.room_id, moment.parameter)
    if target is None:
        return None

    if moment.value is None:
        # Возврат без обычного значения: регулирующее устройство выключается.
        solver.storage.delete_target(moment.room_id, moment.parameter)
        solver._publish(
            bus.TARGET_CHANGED,
            {
                "room_id": moment.room_id,
                "parameter": moment.parameter,
                "value": None,
            },
        )
        device = solver.storage.device(target.device_id)
        if device is None:
            return None
        return solver.execute(
            Command(
                device_id=device.id,
                operation="turn_off",
                source=SOURCE_TARGET,
                actor="расписание целевого значения",
            )
        )

    target.value = moment.value
    solver.storage.save_target(target)
    solver._publish(
        bus.TARGET_CHANGED,
        {
            "room_id": moment.room_id,
            "parameter": moment.parameter,
            "value": moment.value,
        },
    )
    refresh(solver, moment.room_id, moment.parameter)
    return target


def refresh(solver: "Solver", room_id: str, parameter: str) -> None:
    """Сразу пересчитывает решение по текущему показанию.

    Новое целевое значение не ждёт следующего события датчика: иначе
    обогреватель включился бы только через несколько минут после момента
    расписания, и упреждение потеряло бы смысл.
    """
    sensor_type = {
        "temperature": "sensor_temperature",
        "humidity": "sensor_humidity",
    }.get(parameter)
    if sensor_type is None:
        return
    for sensor in solver.storage.devices(room_id):
        if sensor.type != sensor_type:
            continue
        on_reading(solver, sensor)
        return