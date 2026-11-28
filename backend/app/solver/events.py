"""Алгоритм приёма событий и ошибок.

Событие проверяется на корректность, отбрасывается, если оно повторное
или запоздавшее, затем обновляет состояние устройства. Нормальное
событие снимает ошибку устройства, кроме случаев, когда ошибка
снимается только явной командой сброса.

Функция не публикует события сама: она возвращает их список, а ядро
публикует уже после отпускания общей блокировки. Иначе обработчик
публикации, обращающийся к решателю, наткнулся бы на занятую блокировку.
"""

from typing import TYPE_CHECKING, Any, Dict, List, Tuple

from .. import bus
from ..domain import (
    DEVICE_TYPES,
    SENSOR_RANGES,
    STATUS_DONE,
    Event,
)

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Solver

Message = Tuple[str, Any]


def accept(solver: "Solver", event: Event) -> List[Message]:
    """Обрабатывает одно событие. Возвращает список событий шины."""
    storage = solver.storage
    received_at = solver.clock.now()
    messages: List[Message] = []

    device = storage.device(event.device_id)
    if device is None:
        storage.append_history(
            {
                "device_id": event.device_id,
                "kind": "rejected",
                "time": event.time or received_at,
                "received_at": received_at,
                "reason": "устройство не зарегистрировано",
            }
        )
        messages.append(
            (
                bus.EVENT_REJECTED,
                {
                    "device_id": event.device_id,
                    "reason": "устройство не зарегистрировано",
                },
            )
        )
        return messages

    if device.retired:
        return messages

    if not is_valid_value(device.type, event):
        storage.append_history(
            {
                "device_id": device.id,
                "kind": "rejected",
                "time": event.time or received_at,
                "received_at": received_at,
                "value": event.value,
                "reason": "значение вне допустимого диапазона",
            }
        )
        messages.append(
            (
                bus.EVENT_REJECTED,
                {
                    "device_id": device.id,
                    "reason": "значение вне допустимого диапазона",
                },
            )
        )
        return messages

    moment = event.time or received_at

    # Повтор: совпадают устройство, время и вид события.
    if event.event_id and storage.seen_event(event.event_id):
        return messages
    if not event.event_id and storage.history_contains(device.id, event.kind, moment, event.command_id):
        return messages

    history_kind, history_code = event.kind, event.code
    if event.fault_reported:
        history_code = None
        if (device.last_event_time is None or moment >= device.last_event_time) and event.code != device.error:
            history_kind = "error" if event.code is not None else "error_cleared"
            history_code = event.code or device.error
    storage.append_history(
        {
            "device_id": device.id,
            "kind": history_kind,
            "value": event.value,
            "code": history_code,
            "time": moment,
            "received_at": received_at,
            "command_id": event.command_id,
            "event_id": event.event_id,
        }
    )

    if device.last_event_time and moment < device.last_event_time:
        return []
    if event.kind == "error":
        device.last_event_time = moment
        apply_error(solver, device, event, received_at)
        messages.append(
            (
                bus.DEVICE_ERROR,
                {
                    "device_id": device.id,
                    "code": device.error,
                    "room_id": device.room_id,
                },
            )
        )
        return messages

    if event.kind == "state":
        updated = apply_state(solver, device, event, moment, received_at)
        messages.extend(updated)
        return messages

    if event.kind == "cleaning_finished":
        device.state["mode"] = "docked"
        device.state["place"] = None
        device.last_event_time = moment
        device.received_at = received_at
        clear_error_if_needed(device)
        storage.save_device(device)
        messages.append(
            (
                bus.STATE_CHANGED,
                {
                    "device_id": device.id,
                    "state": dict(device.state),
                    "room_id": device.room_id,
                },
            )
        )
        return messages

    if event.kind == "reading":
        updated = apply_reading(solver, device, event, moment, received_at)
        messages.extend(updated)
        return messages

    return messages


def is_valid_value(device_type: str, event: Event) -> bool:
    """Проверяет корректность значения события."""
    if event.kind == "error":
        return True

    if device_type in SENSOR_RANGES:
        if event.kind != "reading":
            return True
        low, high = SENSOR_RANGES[device_type]
        value = event.value
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        # Булевы датчики движения и протечки проверяются отдельно.
        if device_type.startswith("sensor_motion") or device_type.startswith(
            "sensor_leak"
        ):
            return isinstance(value, bool)
        return low <= value <= high

    if device_type in ("sensor_motion", "sensor_leak"):
        return isinstance(event.value, bool)

    spec = DEVICE_TYPES.get(device_type)
    if spec is None:
        return False
    if event.kind == "reading":
        ranges = spec.state_ranges
        if not ranges:
            return True
        property_name = next(iter(ranges))
        low, high = ranges[property_name]
        value = event.value
        if not isinstance(value, (int, float)):
            return False
        return low <= value <= high
    return True


def apply_error(
    solver: "Solver", device: Any, event: Event, received_at: Any
) -> None:
    """Фиксирует ошибку устройства."""
    device.error = event.code or "E10"
    device.received_at = received_at
    solver.storage.save_device(device)


def apply_state(
    solver: "Solver", device: Any, event: Event, moment: Any, received_at: Any
) -> List[Message]:
    """Применяет смену состояния устройства."""
    previous = device.last_event_time
    if previous is not None and moment < previous:
        # Запоздавшее событие сохранено в истории, но состояние не меняет.
        return []

    changes: Dict[str, Any] = event.value if isinstance(event.value, dict) else {}
    device.state.update(changes)
    device.last_event_time = moment
    device.received_at = received_at
    previous_error = device.error
    if event.fault_reported:
        device.error = event.code
    elif not hasattr(solver.model, "dispatch"):
        clear_error_if_needed(device)
    solver.storage.save_device(device)

    messages: List[Message] = []
    messages.extend(confirm_pending(solver, device, changes, event))
    if event.fault_reported and device.error is not None and previous_error != device.error:
        messages.append((bus.DEVICE_ERROR, {"device_id": device.id, "code": device.error, "room_id": device.room_id}))
    messages.append(
        (
            bus.STATE_CHANGED,
            {
                "device_id": device.id,
                "state": dict(device.state),
                "room_id": device.room_id,
                "changes": dict(changes),
            },
        )
    )
    return messages


def apply_reading(
    solver: "Solver", device: Any, event: Event, moment: Any, received_at: Any
) -> List[Message]:
    """Применяет показание датчика."""
    previous = device.last_event_time
    if previous is not None and moment < previous:
        return []

    device.state.update(event.measurements)
    device.state["value"] = event.value
    device.last_event_time = moment
    device.received_at = received_at
    clear_error_if_needed(device)
    solver.storage.save_device(device)

    return [
        (
            bus.READING_RECEIVED,
            {
                "device_id": device.id,
                "value": event.value,
                "room_id": device.room_id,
                "type": device.type,
            },
        )
    ]


def clear_error_if_needed(device: Any) -> None:
    """Снимает ошибку при нормальном событии, если она снимается сама."""
    if device.error is None:
        return
    if device.spec.error_requires_reset:
        return
    device.error = None


def confirm_pending(
    solver: "Solver", device: Any, changes: Dict[str, Any], event: Event
) -> List[Message]:
    """Отмечает команду выполненной по событию модели."""
    messages: List[Message] = []
    if not solver.pending:
        return messages

    for model_id, pending in list(solver.pending.items()):
        if pending.device_id != device.id:
            continue

        # Если модель вернула номер команды, сверка точная.
        if event.command_id is not None:
            if event.command_id != model_id or not matches(pending.expected, changes):
                continue
        elif hasattr(solver.model, "dispatch") or not matches(pending.expected, changes):
            continue

        if solver.clock.now() > pending.deadline:
            continue
        if hasattr(solver.model, 'dispatch') and solver.gateway_acks.get(model_id) is not True:
            solver.gateway_acks[model_id] = event
            continue
        from ..domain import CommandResult

        result = CommandResult(
            STATUS_DONE,
            state=dict(changes),
            model_command_id=model_id,
        )
        solver.confirm(model_id, result)
        if event.event_id:
            solver.journal.write(pending.impact_id, 'шлюз', 'получено состояние симулятора', subject=device.id,
                                 status=STATUS_DONE, reason=f'cmd_id={model_id}, event_id={event.event_id}', at=solver.clock.now())
        messages.append(
            (
                bus.COMMAND_FINISHED,
                {
                    "device_id": device.id,
                    "command_id": model_id,
                    "status": STATUS_DONE,
                    "source": pending.source,
                    "operation": pending.operation,
                },
            )
        )
    return messages


def matches(expected: Dict[str, Any], changes: Dict[str, Any]) -> bool:
    """Проверяет, соответствует ли новое состояние ожидаемому."""
    for property_name, value in expected.items():
        if changes.get(property_name) != value:
            return False
    return True
