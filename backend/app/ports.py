"""Порты внешних компонентов.

Заглушка и настоящий компонент реализуют один и тот же порт, поэтому
ядро решателя не знает, с чем именно работает. Замена выполняется
настройкой HOME_MODEL / STORAGE / KNOWLEDGE / ASSISTANT.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol

from .domain import Command, Device, Event, Room, Target


class Clock(Protocol):
    """Источник времени. В тестах время управляемое."""

    def now(self) -> datetime:
        """Возвращает текущий момент."""


class StoragePort(Protocol):
    """Доступ к состояниям дома."""

    def save_room(self, room: Room) -> None:
        ...

    def delete_room(self, room_id: str) -> None:
        ...

    def rooms(self) -> List[Room]:
        """Возвращает помещения дома."""

    def devices(self, room_id: Optional[str] = None) -> List[Device]:
        """Возвращает устройства, при необходимости только в помещении."""

    def device(self, device_id: str) -> Optional[Device]:
        """Возвращает устройство по идентификатору."""

    def save_device(self, device: Device) -> None:
        """Сохраняет состояние устройства."""

    def targets(self, room_id: Optional[str] = None) -> List[Target]:
        """Возвращает целевые значения."""

    def save_target(self, target: Target) -> None:
        """Сохраняет целевое значение."""

    def delete_target(self, room_id: str, parameter: str) -> None:
        """Удаляет целевое значение."""

    def read_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Возвращает историю событий и ошибок."""

    def append_history(self, record: Dict[str, Any]) -> None:
        """Добавляет запись в историю событий и ошибок."""


class HomeModelPort(Protocol):
    """Модель умного дома: принимает команды и присылает события."""

    def send_command(self, command: Command) -> str:
        """Передаёт команду модели и возвращает её номер."""

    def drain_events(self) -> List[Event]:
        """Возвращает накопившиеся события модели."""


class KnowledgePort(Protocol):
    """Хранилище контекста и знаний."""

    def search(self, query: str, limit: int = 5) -> List[Dict[str, str]]:
        """Ищет фрагменты, относящиеся к вопросу."""


class AssistantPort(Protocol):
    """Интеллектуальный модуль."""

    def ask(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Возвращает ответ и предложенные команды."""