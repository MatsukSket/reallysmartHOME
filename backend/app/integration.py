import os
import secrets
import time
from pathlib import Path
from typing import Literal
from fastapi import Depends, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .catalog import install_catalog, retire_device
from .domain import Command
from .bus import CATALOG_CHANGED
from .home.gateway import IDS, discovered_device, normalize
from .chat.service import install_chat

class Wire(BaseModel):
    event_id:str=Field(min_length=1,max_length=200)
    cmd_id:str|None=None
    device_id:int
    kind:Literal["error","state","telemetry","discovery","removal"]
    ts:float=Field(ge=0,lt=4102444800)
    source:Literal["godot"]
    payload:dict

def install(app,current_session):
    from .api import describe
    install_catalog(app, current_session)
    solver=app.state.solver
    provider=os.getenv("ASSISTANT","demo")
    if provider not in ("demo","ollama"): raise ValueError("ASSISTANT")

    @app.post("/api/gateway/events")
    def event(wire:Wire,x_gateway_token:str=Header(default="")):
        token=os.getenv("GATEWAY_TOKEN")
        if not token or not secrets.compare_digest(token.encode(),x_gateway_token.encode()): raise HTTPException(403)
        if not hasattr(solver.model,"dispatch"): raise HTTPException(409,"Шлюз выключен")
        if wire.ts > time.time()+30: raise HTTPException(422,'Время события в будущем')
        if wire.kind == "removal":
            if wire.device_id in IDS:
                retire_device(solver, IDS[wire.device_id])
            elif 1000 <= wire.device_id <= 2**32 + 999:
                retire_device(solver, f"sim_{wire.device_id}")
            return {"accepted": True, "event_id": wire.event_id}
        if wire.kind == "discovery":
            try:
                new_device = discovered_device(wire.device_id, wire.payload)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            with solver._guard:
                known = solver.storage.device(new_device.id)
                if known is not None and known.type != new_device.type:
                    raise HTTPException(409, "Этот идентификатор занят другим типом устройства")
                if known is None:
                    solver.storage.save_device(new_device)
                    solver.bus.publish(CATALOG_CHANGED, {"subject": new_device.id})
            return {"accepted": True, "event_id": wire.event_id}
        with solver._guard:
            device = solver.storage.device(IDS.get(wire.device_id, f"sim_{wire.device_id}"))
        if device is None:
            raise HTTPException(422, "Устройство отсутствует в каталоге")
        if device.retired:
            return {"accepted": False, "event_id": wire.event_id}
        try: e=normalize(wire, device)
        except (KeyError,ValueError,OverflowError) as exc: raise HTTPException(422,"Некорректные данные") from exc
        accepted=e is not None and solver.accept_event(e)
        if accepted and e.kind == "reading" and e.value is True:
            with solver._guard:
                sensor = solver.storage.device(e.device_id)
                valves = [item.id for item in solver.storage.devices()
                          if item.connected and item.type == "leak_protection"
                          and solver.effective_state(item).get("water") != "closed"]
            if sensor and sensor.type == "sensor_leak" and (sensor.connected or sensor.id == "sensor_leak_bathroom"):
                for valve in valves:
                    solver.execute(Command(valve, "close_water", actor="защита от протечки"), wait=False)
        return {"accepted":accepted,"event_id":wire.event_id}

    install_chat(app, current_session, provider, describe)
    app.mount("/",StaticFiles(directory=os.getenv('FRONTEND_DIR',Path(__file__).resolve().parents[2]/'frontend'/'public'),html=True),name="web")
