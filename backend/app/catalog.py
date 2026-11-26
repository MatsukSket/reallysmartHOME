"""Rooms and enrollment for the devices already present in the home model."""
from uuid import uuid4

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .auth import ROLE_ADMIN
from .bus import CATALOG_CHANGED
from .domain import Room


class RoomRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_id: str | None = Field(max_length=100)


def clear_device_automation(solver, device):
    storage = solver.storage
    device_id = device.id
    for target in storage.targets():
        if device_id in (target.device_id, target.base_device_id):
            storage.delete_target(target.room_id, target.parameter)
    for moment in list(solver.schedules):
        if moment.device_id == device_id:
            solver.delete_schedule(moment.id)
        elif (moment.room_id == device.room_id and device.spec.regulates
              and moment.parameter == device.spec.regulates):
            solver.delete_schedule(moment.id)
    solver.locks.pop(device.id, None)


def retire_device(solver, device_id):
    """Archive enrollment and keep historical observations and audit."""
    with solver._guard:
        device = solver.storage.device(device_id)
        if device is None or device.retired:
            return
        clear_device_automation(solver, device)
        device.connected = False
        device.retired = True
        device.room_id = ""
        solver.storage.save_device(device)
        solver._log(solver.new_impact_id(), "каталог", "удалено устройство",
                    subject=device.name, actor="Godot", status="выполнено")
        solver.bus.publish(CATALOG_CHANGED, {"subject": device_id})


def install_catalog(app, current_session):
    solver = app.state.solver
    storage = solver.storage

    def manager(session=Depends(current_session)):
        if session.role != ROLE_ADMIN:
            raise HTTPException(403, "Каталог может менять только администратор")
        return session

    def record(session, action: str, subject: str):
        solver._log(solver.new_impact_id(), "каталог", action, subject=subject,
                    actor=session.username, status="выполнено")
        solver.bus.publish(CATALOG_CHANGED, {"subject": subject})

    @app.get("/api/catalog/available")
    def available(session=Depends(manager)):
        with solver._guard:
            return {"devices": [
                {"id": device.id, "name": device.name, "type": device.type,
                 "connected": device.connected}
                for device in storage.devices()
                if not device.connected and not device.retired
            ]}

    @app.post("/api/rooms", status_code=201)
    def create_room(request: RoomRequest, session=Depends(manager)):
        name = " ".join(request.name.split())
        if not name:
            raise HTTPException(422, "Введите название комнаты")
        with solver._guard:
            if any(room.name.casefold() == name.casefold() for room in storage.rooms()):
                raise HTTPException(409, "Комната с таким названием уже есть")
            room = Room(id=uuid4().hex, name=name)
            storage.save_room(room)
            record(session, "добавлена комната", room.name)
            return room

    @app.delete("/api/rooms/{room_id}")
    def remove_room(room_id: str, session=Depends(manager)):
        with solver._guard:
            room = storage.room(room_id)
            if room is None:
                raise HTTPException(404, "Комната не найдена")
            if any(device.connected for device in storage.devices(room_id)):
                raise HTTPException(409, "Сначала отключите устройства в этой комнате")
            if storage.targets(room_id) or any(moment.room_id == room_id for moment in solver.schedules):
                raise HTTPException(409, "Сначала удалите автоматизацию комнаты")
            storage.delete_room(room_id)
            record(session, "удалена комната", room.name)
            return {"removed": True}

    @app.put("/api/devices/{device_id}/connection")
    def set_connection(device_id: str, request: ConnectionRequest, session=Depends(manager)):
        room_id = request.room_id
        with solver._device_lock(device_id), solver._guard:
            device = storage.device(device_id)
            if device is None or device.retired:
                raise HTTPException(404, "Устройство отсутствует в каталоге подключения")
            if room_id is not None and storage.room(room_id) is None:
                raise HTTPException(404, "Комната не найдена")
            if device.connected and room_id is not None and room_id != device.room_id:
                raise HTTPException(409, "Сначала отключите устройство от текущей комнаты")
            connected = room_id is not None
            if device.connected == connected:
                return {"id": device.id, "connected": connected, "room_id": room_id}
            if any(command.device_id == device_id for command in solver.pending.values()):
                raise HTTPException(409, "Дождитесь подтверждения текущей команды")

            if not connected:
                clear_device_automation(solver, device)

            device.connected = connected
            device.room_id = room_id or ""
            if device_id == "conditioner_living":
                device.name = "Кондиционер в гостиной" if room_id == "living" else "Кондиционер"
            if device_id == "sensor_temperature_living":
                device.name = "Датчик температуры в гостиной" if room_id == "living" else "Датчик температуры"
            storage.save_device(device)

            record(session, "подключено устройство" if connected else "отключено устройство", device.name)
            return {"id": device.id, "connected": connected, "room_id": room_id}
