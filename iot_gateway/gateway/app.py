"""IoT Gateway: HTTP-API для бэкенда + выбранный адаптер устройств.

Запуск:  GATEWAY_ADAPTER=godot uvicorn gateway.app:app --port 9000
Смена на реальные устройства:  GATEWAY_ADAPTER=zigbee2mqtt MQTT_HOST=... uvicorn gateway.app:app
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .adapters import create_adapter
from .models import Command, CommandResult, CommandStatus
from .pipeline import BackendForwarder, EventPipeline, HttpSender
from .registry import Registry

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def load_settings() -> dict:
    return {
        "adapter": os.getenv("GATEWAY_ADAPTER", "godot"),
        "registry_path": os.getenv("REGISTRY_PATH", "config/devices.json"),
        "backend_events_url": os.getenv("BACKEND_EVENTS_URL", "http://localhost:8000/api/gateway/events"),
        "ack_timeout": float(os.getenv("ACK_TIMEOUT", "5")),
        "godot_host": os.getenv("GODOT_HOST", "0.0.0.0"),
        "godot_port": int(os.getenv("GODOT_PORT", "8765")),
        "mqtt_host": os.getenv("MQTT_HOST", "localhost"),
        "mqtt_port": int(os.getenv("MQTT_PORT", "1883")),
        "mqtt_base_topic": os.getenv("MQTT_BASE_TOPIC", "zigbee2mqtt"),
        "mqtt_user": os.getenv("MQTT_USER"),
        "mqtt_password": os.getenv("MQTT_PASSWORD"),
    }


class CommandBatch(BaseModel):
    commands: list[Command]


def create_app(settings: dict | None = None, sender=None) -> FastAPI:
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        registry = Registry.load(cfg["registry_path"], cfg["adapter"])
        http_sender = sender or HttpSender(cfg["backend_events_url"])
        forwarder = BackendForwarder(http_sender)
        pipeline = EventPipeline(registry, forwarder)
        adapter = create_adapter(cfg["adapter"], registry, pipeline, cfg)
        await forwarder.start()
        await adapter.start()
        app.state.registry, app.state.adapter = registry, adapter
        yield
        await adapter.stop()
        await forwarder.stop()
        if hasattr(http_sender, "close"):
            await http_sender.close()

    app = FastAPI(title="IoT Gateway", lifespan=lifespan)

    @app.get("/health")
    async def health():
        a = app.state.adapter
        return {"adapter": a.name, "transport_connected": a.connected}

    @app.get("/devices")
    async def devices():
        reg: Registry = app.state.registry
        return [{"id": d.id, "type": d.type, "address": reg.native_of(d), "commands": list(d.commands),
                 "rules": d.commands} for d in reg.all()]

    @app.post("/commands")
    async def commands(batch: CommandBatch) -> dict:
        """Принимает список команд, возвращает по каждой статус с квитанцией (confirmed/rejected/timeout/...)."""
        if not batch.commands:
            raise HTTPException(422, "пустой список команд")
        reg: Registry = app.state.registry
        adapter = app.state.adapter

        async def run(cmd: Command) -> CommandResult:
            err = reg.validate_command(cmd)
            if err:
                return CommandResult(cmd_id=cmd.cmd_id, device_id=cmd.device_id,
                                     status=CommandStatus.REJECTED, detail=err)
            return await adapter.execute(cmd)

        results = await asyncio.gather(*(run(c) for c in batch.commands))
        return {"results": [r.model_dump(mode="json") for r in results]}

    if os.getenv("DEMO_UI") == "1" or cfg.get("demo_ui"):
        from . import demo
        demo.install(app)

    return app


app = create_app() if os.getenv("GATEWAY_NO_AUTOAPP") != "1" else None
