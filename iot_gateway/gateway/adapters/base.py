"""Контракт адаптера устройств. Всё, что знает о конкретном протоколе, живёт ТОЛЬКО в наследниках.
Чтобы подключить новый транспорт (Zigbee, Matter, BLE...) достаточно реализовать этот класс."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from ..models import Command, CommandResult, CommandStatus, DeviceEvent
from ..registry import Registry

EventSink = Callable[[DeviceEvent], Awaitable[None]]


class PendingAcks:
    """Таблица ожидающих квитанций: cmd_id -> Future[(status, detail)]."""
    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future] = {}

    def create(self, cmd_id: str) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._futures[cmd_id] = fut
        return fut

    def resolve(self, cmd_id: str, status: CommandStatus, detail: str | None = None) -> bool:
        fut = self._futures.get(cmd_id)
        if fut and not fut.done():
            fut.set_result((status, detail))
            return True
        return False

    def discard(self, cmd_id: str) -> None:
        self._futures.pop(cmd_id, None)

    def fail_all(self, status: CommandStatus, detail: str) -> None:
        for fut in self._futures.values():
            if not fut.done():
                fut.set_result((status, detail))


class DeviceAdapter(ABC):
    name: str = "base"

    def __init__(self, registry: Registry, emit: EventSink, ack_timeout: float = 5.0):
        self.registry = registry
        self.emit = emit                  # куда отдавать нормализованные события
        self.ack_timeout = ack_timeout
        self._acks = PendingAcks()

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    async def _dispatch(self, cmd: Command, native_addr: str) -> None:
        """Отправить команду в нативном формате. Бросает исключение, если отправить не удалось."""

    async def execute(self, cmd: Command) -> CommandResult:
        """Единый жизненный цикл команды: отправка -> ожидание квитанции с тайм-аутом -> статус.
        Повторная отправка автоматически НЕ выполняется (см. требования к решателю)."""
        dev = self.registry.get(cmd.device_id)
        addr = self.registry.native_of(dev) if dev else None
        if addr is None:
            return CommandResult(cmd_id=cmd.cmd_id, device_id=cmd.device_id,
                                 status=CommandStatus.REJECTED, detail="неизвестное устройство")
        if not self.connected:
            return CommandResult(cmd_id=cmd.cmd_id, device_id=cmd.device_id,
                                 status=CommandStatus.UNAVAILABLE, detail="нет связи с транспортом")
        fut = self._acks.create(cmd.cmd_id)
        try:
            await self._dispatch(cmd, addr)
            status, detail = await asyncio.wait_for(fut, self.ack_timeout)
        except asyncio.TimeoutError:
            status, detail = CommandStatus.TIMEOUT, f"нет квитанции за {self.ack_timeout} с"
        except Exception as exc:                                  # noqa: BLE001
            status, detail = CommandStatus.FAILED, f"ошибка отправки: {exc}"
        finally:
            self._acks.discard(cmd.cmd_id)
        return CommandResult(cmd_id=cmd.cmd_id, device_id=cmd.device_id, status=status, detail=detail)
