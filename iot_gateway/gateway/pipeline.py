"""Протоколонезависимая обработка входящих событий: валидация -> дедупликация -> доставка бэкенду."""
from __future__ import annotations

import asyncio
import logging
import time
import os
from collections import deque
from typing import Awaitable, Callable

import httpx

from .models import DeviceEvent, EventKind
from .registry import Registry

log = logging.getLogger("gateway.pipeline")
Sender = Callable[[DeviceEvent], Awaitable[None]]


class HttpSender:
    """Отправка события в FastAPI-бэкенд. Любой не-2xx считается неудачей."""
    def __init__(self, url: str, timeout: float = 3.0):
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout, headers={'X-Gateway-Token':os.getenv('GATEWAY_TOKEN','')})

    async def __call__(self, event: DeviceEvent) -> None:
        r = await self._client.post(self._url, json=event.model_dump(mode="json"))
        r.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()


class BackendForwarder:
    """Очередь доставки. Ошибки — в критическую очередь без лимита и с повторами до успеха;
    телеметрия — в ограниченную очередь (при переполнении вытесняются старые значения)."""
    def __init__(self, sender: Sender, regular_max: int = 1000):
        self._send = sender
        self._critical: deque[DeviceEvent] = deque()
        self._regular: deque[DeviceEvent] = deque(maxlen=regular_max)
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._worker(), name="backend-forwarder")

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._task:
            await self._task

    def submit(self, ev: DeviceEvent, *, critical: bool = False) -> None:
        urgent = critical or ev.kind in (EventKind.ERROR, EventKind.DISCOVERY, EventKind.REMOVAL) or ev.cmd_id or "wet" in ev.payload
        (self._critical if urgent else self._regular).append(ev)
        self._wake.set()

    def _next(self) -> tuple[DeviceEvent, bool] | None:
        if self._critical:
            return self._critical[0], True      # не снимаем, пока не доставлено
        if self._regular:
            return self._regular.popleft(), False
        return None

    async def _worker(self) -> None:
        delay = 0.2
        while True:
            item = self._next()
            if item is None:
                if not self._running:
                    break
                self._wake.clear()
                await self._wake.wait()
                continue
            ev, critical = item
            try:
                await self._send(ev)
                delay = 0.2
                if critical:
                    self._critical.popleft()
            except Exception as exc:                       # noqa: BLE001
                log.warning("доставка %s/%s не удалась: %s", ev.kind.value, ev.device_id, exc)
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 409, 422):
                    # Повтор отклонённого сообщения блокировал бы всю очередь.
                    if critical:
                        self._critical.popleft()
                    delay = 0.2
                    continue
                if not critical:
                    continue                               # телеметрию не повторяем
                if not self._running:
                    break                                  # при остановке не зависаем
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)                # экспоненциальная пауза до 5 с


class EventPipeline:
    def __init__(self, registry: Registry, forwarder: BackendForwarder,
                 deadband: float = 0.05, heartbeat_s: float = 60.0):
        self._reg = registry
        self._fwd = forwarder
        self._deadband = deadband
        self._heartbeat = heartbeat_s
        self._last: dict[tuple[int, EventKind], tuple[dict, float]] = {}

    async def __call__(self, ev: DeviceEvent) -> None:
        if ev.kind is EventKind.REMOVAL:
            self._fwd.submit(ev)
            return
        dev = self._reg.get(ev.device_id)
        if dev is None:
            log.warning("событие от незарегистрированного устройства %s отброшено", ev.device_id)
            return

        if ev.kind is EventKind.ERROR and ev.payload.get("code") == "WATER_LEAK":
            # Ошибка уже означает «мокро» для бэкенда. Следующее состояние
            # нужно передать даже при совпадении с состоянием до аварии.
            self._last.pop((ev.device_id, EventKind.STATE), None)

        if ev.kind in (EventKind.ERROR, EventKind.DISCOVERY) or ev.cmd_id:                     # аварии никогда не фильтруем
            log.info("%s device=%s %s", ev.kind.value, ev.device_id, ev.payload)
            self._fwd.submit(ev)
            return

        if ev.kind is EventKind.TELEMETRY:
            ok, bad = self._reg.filter_telemetry(dev, ev.payload)
            for b in bad:
                log.warning("device=%s невозможное показание отброшено: %s", ev.device_id, b)
            if not ok:
                return
            ev.payload = ok

        previous, _ = self._last.get((ev.device_id, ev.kind), ({}, 0.0))
        fault_changed = ev.kind is EventKind.STATE and "fault" in ev.payload and (
            "fault" not in previous or ev.payload["fault"] != previous["fault"]
        )
        changed = self._dedup(ev)
        if changed is None:
            return
        if not fault_changed:
            ev.payload = changed
        self._fwd.submit(ev, critical=fault_changed)

    def _dedup(self, ev: DeviceEvent) -> dict | None:
        key = (ev.device_id, ev.kind)
        prev, ts = self._last.get(key, ({}, 0.0))
        now = time.monotonic()
        changed = {k: v for k, v in ev.payload.items() if k not in prev or not self._same(prev[k], v)}
        if not changed and now - ts < self._heartbeat:
            return None                                    # ничего нового
        self._last[key] = ({**prev, **ev.payload}, now)
        # Заряд относится к тому же наблюдению, что и признак протечки.
        if "wet" in ev.payload and "battery" in ev.payload:
            return ev.payload
        return changed or ev.payload                       # heartbeat: шлём как есть

    def _same(self, a, b) -> bool:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
                and not isinstance(a, bool) and not isinstance(b, bool):
            return abs(a - b) <= self._deadband
        return a == b
