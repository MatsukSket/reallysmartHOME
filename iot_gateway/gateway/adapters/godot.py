"""Адаптер симуляции Godot. Шлюз — WebSocket-СЕРВЕР, Godot подключается как клиент
(так Godot можно перезапускать, а шлюз остаётся на постоянном адресе).

Godot -> шлюз (JSON):
  {"type":"telemetry","node":"Kitchen/Boiler","values":{"temperature":65.2}}
  {"type":"state","node":"Kitchen/Boiler","values":{"power":"on","target_temp":60}}
  {"type":"error","node":"Kitchen/Boiler","code":"E04","message":"..."}
  {"type":"ack","cmd_id":"...","ok":true,"error":null}
  любое сообщение может нести "msg_id" -> шлюз ответит {"type":"received","msg_id":...}
Шлюз -> Godot:
  {"type":"command","cmd_id":"...","node":"Kitchen/Boiler","action":"set_state","params":{...}}
"""
from __future__ import annotations

import json
import logging
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from ..models import Command, CommandStatus, DeviceEvent, EventKind
from .base import DeviceAdapter

log = logging.getLogger("gateway.godot")


class GodotAdapter(DeviceAdapter):
    name = "godot"

    def __init__(self, *args: Any, host: str = "0.0.0.0", port: int = 8765, **kw: Any):
        super().__init__(*args, **kw)
        self._host, self.port = host, port
        self._server = None
        self._ws: ServerConnection | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def start(self) -> None:
        self._server = await serve(self._handler, self._host, self.port, ping_interval=10, ping_timeout=10)
        self.port = self._server.sockets[0].getsockname()[1]      # актуально и для port=0 в тестах
        log.info("ожидаю подключения Godot на ws://%s:%s", self._host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ---------- приём от Godot ----------
    async def _handler(self, ws: ServerConnection) -> None:
        if self._ws is not None:
            log.warning("новое подключение Godot вытесняет старое")
            await self._ws.close()
        self._ws = ws
        log.info("Godot подключён")
        try:
            async for raw in ws:
                await self._on_message(ws, raw)
        finally:
            if self._ws is ws:
                self._ws = None
                self._acks.fail_all(CommandStatus.UNAVAILABLE, "соединение с Godot потеряно")
            log.info("Godot отключён")

    async def _on_message(self, ws: ServerConnection, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
            mtype = msg["type"]
        except (ValueError, KeyError, TypeError):
            log.warning("некорректное сообщение от Godot отброшено: %.200r", raw)
            return

        if mtype == "ack":
            ok = bool(msg.get("ok"))
            self._acks.resolve(str(msg.get("cmd_id")),
                               CommandStatus.CONFIRMED if ok else CommandStatus.REJECTED,
                               None if ok else str(msg.get("error") or "отклонено устройством"))
            return
        if mtype == "hello":
            log.info("Godot hello: %s", msg.get("version", "?"))
            return

        dev = self.registry.by_native(str(msg.get("node")))
        if dev is None:
            log.warning("неизвестный узел Godot: %r", msg.get("node"))
            return

        if mtype in ("telemetry", "state"):
            payload = msg.get("values")
            kind = EventKind.TELEMETRY if mtype == "telemetry" else EventKind.STATE
        elif mtype == "error":
            payload = {"code": str(msg.get("code", "UNKNOWN")), "message": msg.get("message", "")}
            kind = EventKind.ERROR
        else:
            log.warning("неизвестный тип сообщения: %r", mtype)
            return
        if not isinstance(payload, dict):
            log.warning("поле values/payload должно быть объектом: %.200r", msg)
            return

        await self.emit(DeviceEvent(device_id=dev.id, kind=kind, payload=payload, source=self.name,
                                    **({"ts": msg["ts"]} if isinstance(msg.get("ts"), (int, float)) else {})))
        if "msg_id" in msg:                                       # квитанция о получении
            await ws.send(json.dumps({"type": "received", "msg_id": msg["msg_id"]}))

    # ---------- отправка в Godot ----------
    async def _dispatch(self, cmd: Command, native_addr: str) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps({
            "type": "command", "cmd_id": cmd.cmd_id, "node": native_addr,
            "action": cmd.command, "params": cmd.params,
        }))
