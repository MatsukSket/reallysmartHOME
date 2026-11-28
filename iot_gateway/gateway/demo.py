"""Демо-панель: показывает живые события и позволяет слать команды из браузера.
Включается переменной DEMO_UI=1. Заодно играет роль «бэкенда»: принимает события шлюза
на POST /api/gateway/events (в бою это делает ваш FastAPI)."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse

from .models import DeviceEvent, EventKind

STATIC = Path(__file__).parent / "static"


class DemoHub:
    def __init__(self) -> None:
        self.devices: dict[int, dict] = {}
        self.retired: set[int] = set()
        self.subscribers: set[asyncio.Queue] = set()

    def apply(self, ev: DeviceEvent) -> None:
        if ev.kind is EventKind.REMOVAL:
            self.retired.add(ev.device_id)
            self.devices.pop(ev.device_id, None)
        elif ev.device_id in self.retired:
            return
        else:
            d = self.devices.setdefault(ev.device_id, {"state": {}, "telemetry": {}, "last_error": None})
            if ev.kind is EventKind.STATE:
                d["state"].update(ev.payload)
                if "fault" in ev.payload:
                    code = ev.payload["fault"]
                    d["last_error"] = {"code": code} if code else None
            elif ev.kind is EventKind.TELEMETRY:
                d["telemetry"].update(ev.payload)
            elif ev.kind is EventKind.ERROR:
                d["last_error"] = ev.payload
        msg = json.dumps(ev.model_dump(mode="json"))
        for q in list(self.subscribers):
            q.put_nowait(msg)


def install(app: FastAPI) -> None:
    hub = DemoHub()
    app.state.demo = hub
    r = APIRouter()

    @r.post("/api/gateway/events")
    async def receive(ev: DeviceEvent):
        hub.apply(ev)
        return {"ok": True}

    @r.get("/api/demo/snapshot")
    async def snapshot():
        return {"devices": hub.devices, "retired": sorted(hub.retired), "read_only": bool(os.getenv("GATEWAY_TOKEN"))}

    @r.get("/api/demo/stream")
    async def stream(request: Request):
        q: asyncio.Queue = asyncio.Queue()
        hub.subscribers.add(q)

        async def gen():
            try:
                yield ": connected\n\n"
                while not await request.is_disconnected():
                    try:
                        yield f"data: {await asyncio.wait_for(q.get(), 15)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                hub.subscribers.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @r.get("/ui")
    async def ui():
        return FileResponse(STATIC / "dashboard.html")

    app.include_router(r)
