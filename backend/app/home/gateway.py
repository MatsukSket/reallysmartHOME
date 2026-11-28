"""Адаптер локальной симуляции; подтверждение состояния приходит отдельным событием."""
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
import json
import httpx
from ..domain import Device, Event

IDS = {123: "boiler", 124: "conditioner_living", 125: "leak_protection", 126: "sensor_leak_bathroom", 127: "light_spare", 128: "sensor_temperature_living"}
NODES = ["Kitchen/Boiler", "Living/AC", "Bath/WaterValve", "Bath/LeakSensor", "Spare/Lamp", "Living/Thermometer"]

DISCOVERY_TYPES = {
    "boiler": "boiler", "air_conditioner": "conditioner", "water_valve": "leak_protection",
    "leak_sensor": "sensor_leak", "light": "light", "temperature_sensor": "sensor_temperature",
}
STATIC_TYPES = {123: "boiler", 124: "conditioner", 125: "leak_protection", 126: "sensor_leak", 127: "light", 128: "sensor_temperature"}
FAULT_CODES = {
    DISCOVERY_TYPES[kind]: frozenset(codes)
    for kind, codes in json.loads(
        (Path(__file__).resolve().parents[3] / "godot_sim/config/errors.json").read_text(encoding="utf-8")
    ).items()
}


def discovered_device(ident, payload):
    name = payload.get("name")
    kind = payload.get("type")
    if not 1000 <= ident <= 2**32 + 999 or not isinstance(kind, str) or kind not in DISCOVERY_TYPES:
        raise ValueError("Тип устройства не поддерживается")
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise ValueError("Некорректное название устройства")
    return Device(id=f"sim_{ident}", name=name.strip(), type=DISCOVERY_TYPES[kind],
                  room_id="", connected=False, model={"boiler": "Baxi Eco 4s", "air_conditioner": "Godot AC"}.get(kind, ""))


def simulation_house(house):
    registry = json.loads((Path(__file__).resolve().parents[3] / "iot_gateway/config/devices.json").read_text(encoding="utf8"))
    if {d["id"]: d["native"]["godot"] for d in registry["devices"]} != dict(zip(IDS, NODES)):
        raise ValueError("Каталог шлюза не соответствует отображению симуляции")
    ac = next(d.copy() for d in house["devices"] if d["type"] == "conditioner")
    ac.update(id=IDS[124], room="living", name="Кондиционер в гостиной", model="Godot AC")
    devices = [d.copy() for d in house["devices"] if d["id"] in IDS.values()] + [ac]
    next(d for d in devices if d["id"] == "boiler").update(room="kitchen", model="Baxi Eco 4s")
    devices.append({
        "id": IDS[127], "name": "Запасная лампа", "type": "light",
        "room": "", "connected": False, "state": {"power": False, "brightness": 60},
    })
    return {"rooms":[r for r in house["rooms"] if r["id"] in {"kitchen","bathroom","living"}],"devices":devices}

class GatewayHome:
    def __init__(self, storage):
        self.storage = storage

    def send_command(self, command):
        return uuid.uuid4().hex

    def dispatch(self, command, cmd_id):
        device = self.storage.device(command.device_id)
        params = device.spec.operation(command.operation).desired(command.params)
        if set(params) == {"water"}:
            params = {"power": "on" if params["water"] == "open" else "off"}
        elif set(params) == {"power"}:
            params = {"power": "on" if params["power"] else "off"}
        elif set(params) == {"coolant_temperature"}:
            params = {"target_temp":params["coolant_temperature"]}
        elif set(params) not in ({"target_temp"}, {"mode"}, {"brightness"}):
            return "rejected", "Операция недоступна в симуляции"
        if device.id.startswith("sim_"):
            ident = int(device.id.removeprefix("sim_"))
        else:
            ident = next(i for i, name in IDS.items() if name == device.id)
        response=httpx.post(os.getenv("GATEWAY_URL","http://127.0.0.1:9000")+"/commands",
            headers={"X-Gateway-Token":os.getenv("GATEWAY_TOKEN","")},json={"commands":[{"device_id":ident,"cmd_id":cmd_id,"params":params}]},timeout=8)
        response.raise_for_status()
        result, = response.json()["results"]
        if result["cmd_id"] != cmd_id or result["device_id"] != ident:
            raise ValueError("Некорректная квитанция")
        return result["status"], result.get("detail")

    def drain_events(self):
        return []

def normalize(wire, device=None):
    ident, data = wire.device_id, wire.payload
    kind = device.type if device else STATIC_TYPES[ident]
    kwargs = dict(device_id=device.id if device else IDS[ident], time=datetime.fromtimestamp(wire.ts,timezone.utc).replace(tzinfo=None),
                  command_id=wire.cmd_id,event_id=wire.event_id)
    if kind=="boiler" and wire.kind=="state" and "target_temp" in data:
        if type(data["target_temp"]) not in (int,float) or not 30<=data["target_temp"]<=80: raise ValueError("target_temp")
    if kind=="conditioner" and "target_temp" in data:
        if type(data["target_temp"]) not in (int,float) or not 16<=data["target_temp"]<=30: raise ValueError("target_temp")
    if wire.kind=="error":
        if kind=="sensor_leak" and data.get("code")=="WATER_LEAK":
            return Event(kind="reading", value=True, **kwargs)
        return Event(kind="error", code=str(data["code"]), **kwargs)
    if kind == "sensor_temperature" and "temperature" in data:
        return Event(kind="reading", value=data["temperature"], **kwargs)
    if kind=="sensor_leak" and isinstance(data.get("wet"),bool):
        battery = data.get("battery")
        measurements = {"battery": battery} if type(battery) in (int, float) and 0 <= battery <= 100 else {}
        return Event(kind="reading", value=data["wet"], measurements=measurements, **kwargs)
    changes={}
    if "power" in data:
        if data["power"] not in ("on","off"): raise ValueError("power")
        if kind=="leak_protection": changes["water"]="open" if data["power"]=="on" else "closed"
        else: changes["power"]=data["power"]=="on"
    for key in ("temperature","pressure","target_temp","mode","humidity","brightness","mains","jammed"):
        if key in data:
            changes["coolant_temperature" if kind=="boiler" and key=="target_temp" else key]=data[key]
    if kind=="sensor_leak": return None
    fault_reported = wire.kind == "state" and "fault" in data
    fault = data.get("fault") if fault_reported else None
    if fault_reported and (kind not in FAULT_CODES or (
            fault is not None and (not isinstance(fault, str) or fault not in FAULT_CODES[kind]))):
        raise ValueError("fault")
    if changes or fault_reported:
        return Event(kind="state", value=changes, code=fault, fault_reported=fault_reported, **kwargs)
    return None
