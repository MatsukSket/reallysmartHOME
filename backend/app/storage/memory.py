"""Заглушка хранилища состояний.

Обычные словари в памяти, заполняемые из house.yaml. После перезапуска
всё сбрасывается к исходному описанию дома: для разработки и тестов это
удобно. Режим unavailable воспроизводит ошибочный сценарий «хранилище
недоступно».
"""

from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..domain import Device, Room, Target


class StorageUnavailable(RuntimeError):
    """Хранилище недоступно."""


def _copy_device(device: Device) -> Device:
    """Возвращает копию устройства, чтобы наружу не уходила ссылка."""
    return replace(device, state=dict(device.state))


class MemoryStorage:
    """Хранилище в памяти с тем же портом, что и настоящая база данных."""

    def __init__(self, house: Dict[str, Any], unavailable: bool = False) -> None:
        self._unavailable = unavailable
        self._rooms: Dict[str, Room] = {}
        self._devices: Dict[str, Device] = {}
        self._targets: Dict[tuple, Target] = {}
        self._event_ids = set()
        self._history: List[Dict[str, Any]] = []
        self._load(house)

    def _load(self, house: Dict[str, Any]) -> None:
        for item in house.get("rooms", []):
            room = Room(id=item["id"], name=item["name"])
            self._rooms[room.id] = room
        for item in house.get("devices", []):
            device = Device(
                id=item["id"],
                name=item["name"],
                type=item["type"],
                room_id=item["room"],
                state=dict(item.get("state", {})),
                connected=item.get("connected", True),
                model=item.get("model", ""),
            )
            self._devices[device.id] = device

    def _check(self) -> None:
        if self._unavailable:
            raise StorageUnavailable("хранилище недоступно")

    def set_unavailable(self, value: bool) -> None:
        """Переключает режим недоступности хранилища."""
        self._unavailable = value

    def rooms(self) -> List[Room]:
        """Возвращает помещения дома."""
        self._check()
        return list(self._rooms.values())

    def room(self, room_id: str) -> Optional[Room]:
        """Возвращает помещение по идентификатору."""
        self._check()
        return self._rooms.get(room_id)

    def save_room(self, room: Room) -> None:
        self._check()
        self._rooms[room.id] = room

    def delete_room(self, room_id: str) -> None:
        self._check()
        self._rooms.pop(room_id, None)

    def devices(self, room_id: Optional[str] = None) -> List[Device]:
        """Возвращает устройства, при необходимости только в помещении."""
        self._check()
        items = [
            _copy_device(device)
            for device in self._devices.values()
            if room_id is None or device.room_id == room_id
        ]
        return items

    def device(self, device_id: str) -> Optional[Device]:
        """Возвращает устройство по идентификатору."""
        self._check()
        device = self._devices.get(device_id)
        return _copy_device(device) if device else None

    def save_device(self, device: Device) -> None:
        """Сохраняет состояние устройства."""
        self._check()
        self._devices[device.id] = _copy_device(device)

    def targets(self, room_id: Optional[str] = None) -> List[Target]:
        """Возвращает целевые значения."""
        self._check()
        return [
            target
            for target in self._targets.values()
            if room_id is None or target.room_id == room_id
        ]

    def save_target(self, target: Target) -> None:
        """Сохраняет целевое значение, заменяя прежнее для того же параметра."""
        self._check()
        self._targets[(target.room_id, target.parameter)] = target

    def delete_target(self, room_id: str, parameter: str) -> None:
        """Удаляет целевое значение."""
        self._check()
        self._targets.pop((room_id, parameter), None)

    def read_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Возвращает историю событий и ошибок."""
        self._check()
        return list(self._history[-limit:])

    def append_history(self, record: Dict[str, Any]) -> None:
        """Добавляет запись в историю событий и ошибок."""
        self._check()
        self._history.append(record)
        if record.get('event_id'): self._event_ids.add(record['event_id'])

    def seen_event(self, event_id):
        return event_id in self._event_ids

    def history_contains(
        self,
        device_id: str,
        kind: str,
        moment: datetime,
        command_id: Optional[str] = None,
    ) -> bool:
        """Проверяет, есть ли уже такое событие.

        Событие считается повтором, если совпадают устройство, вид и
        время. Если модель вернула номер команды, он тоже входит в
        сравнение: две разные команды могли выполниться в одну секунду.
        """
        self._check()
        for record in self._history:
            if record.get("device_id") != device_id:
                continue
            if record.get("kind") != kind:
                continue
            if record.get("time") != moment:
                continue
            if command_id is not None and record.get("command_id") != command_id:
                continue
            return True
        return False