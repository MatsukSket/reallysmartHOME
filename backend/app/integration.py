import os
import re
import secrets
import threading
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
from .rag.retriever import Knowledge
from .rag.assistant import Ollama

class Wire(BaseModel):
    event_id:str=Field(min_length=1,max_length=200)
    cmd_id:str|None=None
    device_id:int
    kind:Literal["error","state","telemetry","discovery","removal"]
    ts:float=Field(ge=0,lt=4102444800)
    source:Literal["godot"]
    payload:dict

class Question(BaseModel):
    message:str=Field(min_length=1,max_length=2000)

def install(app,current_session):
    from .api import describe
    install_catalog(app, current_session)
    solver=app.state.solver
    tokens={}
    lock=threading.Lock()
    provider=os.getenv("ASSISTANT","demo")
    if provider not in ("demo","ollama"): raise ValueError("ASSISTANT")
    knowledge=Knowledge(provider=="ollama")

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

    @app.get("/api/mode")
    def mode(session=Depends(current_session)):
        return {"assistant":provider}

    @app.get("/api/sources/{chunk_id}")
    def source(chunk_id:str,session=Depends(current_session)):
        from .rag.indexer import passages
        for chunk in passages():
            if chunk["chunk_id"]==chunk_id: return chunk
        raise HTTPException(404,"Фрагмент не найден")

    @app.post("/api/chat")
    def chat(q:Question,session=Depends(current_session)):
        text=q.message.lower().strip(" .!?")
        def reply(message,kind="info",**kw):
            return dict(reply=message,kind=kind,provider=provider,sources=[],**kw)
        # Узкий словарь действий демонстрации. Свободный ответ модели не исполняется.
        actions={"включи кондиционер":("conditioner_living","turn_on"),"выключи кондиционер":("conditioner_living","turn_off"),
                 "перекрой воду":("leak_protection","close_water"),"открой воду":("leak_protection","open_water")}
        if text in actions:
            ident,op=actions[text]
            dev=solver.storage.device(ident)
            if not dev or not dev.connected or not solver.is_fresh(dev): return reply("Нет свежего состояния устройства","degraded")
            if provider=="ollama":
                try:
                    docs=knowledge.search(q.message+" "+dev.room_id+" "+ident,3)
                    Ollama().ask(dict(question=q.message,state=dev.state,documents=docs,
                        allowed_command=dict(device_id=ident,action="SET",params={"operation":op})))
                except Exception:
                    return reply("Модель или поиск не подтвердили допустимое действие.","degraded")
            if ident=="leak_protection":
                token=secrets.token_urlsafe(24)
                with lock:
                    for k in [k for k,v in tokens.items() if v[1]<=time.monotonic()]: tokens.pop(k)
                    tokens[token]=(session.username,time.monotonic()+60,ident,op,session.role)
                return reply("Подтвердите действие: "+text,"confirm",confirmation=token)
            return reply("Результат команды","action",result=describe(solver.execute(Command(ident,op,source="assistant",actor=session.username))))
        if re.match(r"(включ|выключ|откр|закр|перекр)",text):
            return reply("Уточните устройство и действие, например «включи кондиционер».","clarify")
        devices=[device for device in solver.storage.devices() if device.connected]
        context=[dict(id=d.id,room=d.room_id,state=d.state,error=d.error,stale=not solver.is_fresh(d)) for d in devices]
        if "температур" in text and "гостин" in text and "ошиб" not in text:
            temp = next((d for d in devices if d.type == "sensor_temperature" and d.room_id == "living"
                         and solver.is_fresh(d) and d.state.get("value") is not None), None)
            if temp is None: return reply("Нет актуальных показаний температуры.","degraded")
            return reply(f"В гостиной {temp.state.get('value')} °C по данным датчика.")
        query=q.message+" "+" ".join(f"{d.room_id} {d.id} {d.error}" for d in devices if d.error)
        try:
            docs=knowledge.search(query,3)
            codes=re.findall(r"\bE\d{2}\b",query.upper())
            if codes: docs=[d for d in docs if d["code"] in codes and d["model"]=="Baxi Eco 4s"]
            if not docs: return reply("Подходящая инструкция отсутствует.","degraded")
            if provider=="ollama": answer=Ollama().ask(dict(question=q.message,home=context,documents=docs))["reply"]
            else:
                best=docs[0]
                answer=best["text"].split("Действие:")[0].strip()
                if best["code"]: answer+=(" По данным дома ошибка активна." if any(d.error==best["code"] for d in devices) else " Это справка из руководства.")
            if not answer.strip(): raise ValueError("Пустой ответ")
            return dict(reply=answer,kind="info",provider=provider,sources=[d["source"]+"#"+d["chunk_id"] for d in docs])
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Ошибка при запросе справки: %s",type(exc).__name__)
            return reply("Поиск или модель недоступны. Ручное управление доступно.","degraded")

    @app.post("/api/chat/confirm/{confirmation_id}")
    def confirm(confirmation_id:str,session=Depends(current_session)):
        token=confirmation_id
        with lock:
            entry=tokens.get(token)
            if not entry or entry[0]!=session.username or entry[4]!=session.role: raise HTTPException(403,"Подтверждение недоступно")
            tokens.pop(token)
        if time.monotonic()>entry[1]: raise HTTPException(410,"Подтверждение истекло")
        if not solver.is_fresh(solver.storage.device(entry[2])): raise HTTPException(409,"Состояние устарело")
        return describe(solver.execute(Command(entry[2],entry[3],source="assistant",actor=session.username)))

    app.mount("/",StaticFiles(directory=os.getenv('FRONTEND_DIR',Path(__file__).resolve().parents[2]/'frontend'/'public'),html=True),name="web")
