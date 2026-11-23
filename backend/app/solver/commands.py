"""Алгоритм проверки и выполнения управляющей команды.

Единый путь для всех команд: ручных, подтверждённых предложений
помощника, команд целевого значения и расписания. Источник влияет
только на приоритет, проверку блокировки и постановку блокировки после
выполнения.

Проверка отделена от отправки. Проверка выполняется под блокировкой
решателя, а отправка команды модели --- уже без неё: обращение к
внешнему компоненту может быть долгим, и держать на это время общую
блокировку нельзя. Очередь к одному устройству обеспечивает отдельная
блокировка устройства, а не общая.
"""

from typing import TYPE_CHECKING, Any, Dict, Optional, Union

from ..config import STALE_AFTER_S, VACUUM_MIN_CHARGE
from ..domain import (
    SOURCE_SCHEDULE,
    SOURCE_TARGET,
    STATUS_NOT_REQUIRED,
    STATUS_REJECTED,
    Command,
    CommandResult,
    Device,
)

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Solver


class Prepared:
    """Проверенная команда, готовая к отправке модели."""

    def __init__(
        self, device_id: str, operation: str, expected: Dict[str, Any]
    ) -> None:
        self.device_id = device_id
        self.operation = operation
        self.expected = expected


def prepare(
    solver: "Solver", command: Command
) -> Union[CommandResult, Prepared]:
    """Проверяет команду. Возвращает либо итог, либо готовую к отправке."""
    storage = solver.storage

    # 1. Устройство существует.
    try:
        device = storage.device(command.device_id)
    except Exception:
        return reject("хранилище состояний недоступно, команда не выполнена")
    if device is None:
        return reject("устройство не найдено")

    # 2. Операция поддерживается устройством.
    spec = device.spec.operation(command.operation)
    if spec is None:
        return reject("операция не поддерживается устройством")

    # 3. Параметры допустимы.
    problem = spec.validate(command.params)
    if problem is not None:
        return reject(problem)

    # 4. Устройство не находится в неисправимой ошибке.
    if device.error is not None and device.spec.error_requires_reset:
        if not spec.requires_error:
            return reject(
                "устройство в состоянии ошибки,"
                " требуется сброс ошибки в окне устройства"
            )

    # 5. Операция, допустимая только при ошибке.
    if spec.requires_error and device.error is None:
        return CommandResult(STATUS_NOT_REQUIRED, "ошибки нет")

    # 6. Проверки безопасности по показаниям датчиков.
    safety = check_safety(solver, device, command)
    if safety is not None:
        return safety

    # 7. Требуемое состояние.
    desired = spec.desired(command.params)

    # 8. Команда уже выполнена. Учитывается не только сохранённое
    # состояние, но и ожидаемое: иначе две одновременные команды к
    # одному устройству ушли бы модели дважды.
    effective = solver.effective_state(device)
    if command.operation != "reset_error" and is_satisfied(effective, desired):
        return CommandResult(STATUS_NOT_REQUIRED, "устройство уже в этом состоянии")

    # 9. Конфликт с блокировкой.
    if command.source in (SOURCE_TARGET, SOURCE_SCHEDULE):
        lock = solver.lock_of(device.id)
        if lock is not None:
            for property_name, value in desired.items():
                if lock.contradicts(property_name, value):
                    return reject("команда противоречит ручному управлению устройством")

    expected: Dict[str, Any] = (
        {} if command.operation == "reset_error" else dict(desired)
    )
    return Prepared(
        device_id=device.id, operation=command.operation, expected=expected
    )


def reject(reason: str) -> CommandResult:
    """Возвращает результат «отклонено»."""
    return CommandResult(STATUS_REJECTED, reason)


def is_satisfied(state: Dict[str, Any], desired: Dict[str, Any]) -> bool:
    """Проверяет, что устройство уже находится в требуемом состоянии."""
    for property_name, value in desired.items():
        if state.get(property_name) != value:
            return False
    return True


def check_safety(
    solver: "Solver", device: Device, command: Command
) -> Optional[CommandResult]:
    """Выполняет проверки безопасности по показаниям датчиков.

    Проверки не должны опираться на устаревшие сведения: если датчик
    молчит дольше допустимого, решение об открытии воды или запуске
    пылесоса не принимается.
    """
    # Открывать воду можно, только если ни один датчик протечки не мокрый.
    if device.type == "leak_protection" and command.operation == "open_water":
        for sensor in solver.storage.devices():
            if sensor.type != "sensor_leak":
                continue
            room = solver.storage.room(sensor.room_id)
            place = room.name if room else sensor.room_id
            if not solver.is_fresh(sensor, STALE_AFTER_S):
                return reject(
                    "нет актуальных сведений от датчика протечки в помещении"
                    " «{}», открывать воду небезопасно".format(place)
                )
            if sensor.state.get("value") is True:
                return reject(
                    "датчик протечки в помещении «{}» всё ещё фиксирует"
                    " протечку".format(place)
                )

    # Пылесос не запускается с низким зарядом и в помещении с протечкой.
    if device.type == "vacuum" and command.operation == "start_cleaning":
        charge = device.state.get("charge")
        if isinstance(charge, (int, float)) and charge < VACUUM_MIN_CHARGE:
            return reject("недостаточно заряда для уборки")

        room_id = command.params.get("room_id")
        if room_id is not None:
            if solver.storage.room(room_id) is None:
                return reject("помещение для уборки не найдено")
            for sensor in solver.storage.devices(room_id):
                if sensor.type != "sensor_leak":
                    continue
                if not solver.is_fresh(sensor, STALE_AFTER_S):
                    return reject(
                        "нет актуальных сведений о протечке в помещении,"
                        " запуск уборки небезопасен"
                    )
                if sensor.state.get("value") is True:
                    return reject("в помещении зафиксирована протечка")

    return None