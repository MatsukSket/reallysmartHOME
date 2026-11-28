"""Эмулятор Godot: 6 устройств из config/devices.json, телеметрия раз в секунду, квитирование команд.
Консоль: e = E04, r = восстановить давление, c = снять ошибку котла, l = протечка, d = высушить датчик.
Можно также ввести код ошибки или clear ac|valve|lamp для сброса на другом приборе.
Запуск:   python tools/fake_godot.py [ws://127.0.0.1:8765]"""
import asyncio
import json
import random
import sys
from pathlib import Path
from threading import Thread

from websockets.exceptions import ConnectionClosed
from websockets.asyncio.client import connect

DEFAULT_URL = "ws://127.0.0.1:8765"
DEV = {
    "Kitchen/Boiler":  {"state": {"power": "off", "target_temp": 50.0}, "temp": 20.0, "pressure": 1.4, "fault": ""},
    "Living/AC":       {"state": {"power": "off", "target_temp": 22.0, "mode": "auto"}, "temp": 25.0},
    "Bath/WaterValve": {"state": {"power": "on"}},
    "Bath/LeakSensor": {"state": {"wet":False}, "humidity": 40.0},
    "Spare/Lamp": {"state": {"power": "off", "brightness": 60}},
    "Living/Thermometer": {"state": {}},
}
RECEIPTS = {}
RULES = {"Kitchen/Boiler": (30, 80), "Living/AC": (16, 30)}
EXECUTORS = {
    "boiler": ("Kitchen/Boiler", "boiler"),
    "ac": ("Living/AC", "air_conditioner"),
    "valve": ("Bath/WaterValve", "water_valve"),
    "lamp": ("Spare/Lamp", "light"),
}
ERROR_CATALOG = json.loads(
    (Path(__file__).resolve().parents[2] / "godot_sim/config/errors.json").read_text(encoding="utf-8")
)
ERROR_NODES = {code: node for node, kind in EXECUTORS.values() for code in ERROR_CATALOG[kind]}
FAULT_NODES = {node for node, _ in EXECUTORS.values()}


async def session(ws, console_commands):
    async def send(**m): await ws.send(json.dumps(m))
    async def send_state(node, **extra):
        device = DEV[node]
        values = dict(device["state"])
        if node in FAULT_NODES:
            values["fault"] = device.get("fault") or None
        await send(type="state", node=node, values=values, **extra)

    for node, d in DEV.items():                                   # полное состояние после подключения
        if d["state"]: await send_state(node)

    async def sim():
        while True:
            for node in ("Kitchen/Boiler", "Living/AC"):
                d, s = DEV[node], DEV[node]["state"]
                ambient = 20.0 if node == "Kitchen/Boiler" else 25.0
                goal = ambient
                if s["power"] == "on" and s.get("mode") != "fan":
                    goal = s["target_temp"]
                    if s.get("mode") == "cool":
                        goal = min(ambient, goal)
                    elif s.get("mode") == "heat":
                        goal = max(ambient, goal)
                d["temp"] += max(-0.8, min(0.8, goal - d["temp"])) * 0.5 + random.uniform(-0.05, 0.05)
                values = {"temperature": round(d["temp"], 1)}
                if "pressure" in d:
                    values["pressure"] = d["pressure"]
                await send_state(node)
                await send(type="telemetry", node=node, values=values)
            await send(type="telemetry", node="Living/Thermometer",
                       values={"temperature": round(DEV["Living/AC"]["temp"], 1)})
            h = DEV["Bath/LeakSensor"]; h["humidity"] = max(30, min(95, h["humidity"] + random.uniform(-1, 1)))
            await send(type="telemetry", node="Bath/LeakSensor", values={"humidity": round(h["humidity"], 1)})
            for node in ("Bath/LeakSensor", "Bath/WaterValve", "Spare/Lamp"):
                await send_state(node)
            await asyncio.sleep(1)

    async def keyboard():
        while True:
            line = await console_commands.get()
            if line is None:
                return
            boiler = DEV["Kitchen/Boiler"]
            code = "E04" if line == "e" else line.upper()
            if code in ERROR_NODES:
                node = ERROR_NODES[code]
                device = DEV[node]
                if code == "E04":
                    boiler["pressure"] = 0.4
                device["fault"] = "E04" if node == "Kitchen/Boiler" and boiler["pressure"] < 1 else code
                if node != "Bath/WaterValve":
                    device["state"]["power"] = "off"
                await send_state(node)
                if code == "E04":
                    await send(type="telemetry", node=node, values={"pressure": boiler["pressure"]})
            elif line == "r":
                boiler["pressure"] = 1.4
                await send(type="telemetry", node="Kitchen/Boiler", values={"pressure": boiler["pressure"]})
            elif line == "c" or line.startswith("clear "):
                key = "boiler" if line == "c" else line.removeprefix("clear ")
                if key in EXECUTORS and (key != "boiler" or boiler["pressure"] >= 1.0):
                    node, _ = EXECUTORS[key]
                    DEV[node]["fault"] = ""
                    await send_state(node)
            elif line == "d":
                sensor = DEV["Bath/LeakSensor"]
                sensor["state"]["wet"] = False
                await send_state("Bath/LeakSensor")
            elif line == "l":
                DEV["Bath/LeakSensor"]["state"]["wet"]=True
                await send(type="error", node="Bath/LeakSensor", code="WATER_LEAK", message="Обнаружена протечка")

    async def commands():
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "command": continue
            node, p = msg["node"], msg.get("params", {})
            print("← команда:", node, p)
            if msg["cmd_id"] in RECEIPTS:
                await send(**RECEIPTS[msg["cmd_id"]]); continue
            d = DEV.get(node); err = None
            if d is None: err = "узел не найден"
            elif "target_temp" in p and not (RULES[node][0] <= p["target_temp"] <= RULES[node][1]): err = "уставка вне диапазона"
            elif "reset_error" in p: err = "Сброс доступен только в симуляторе"
            elif d.get("fault") and not (node == "Bath/WaterValve" and d["fault"] == "E22" and p.get("power") == "off"):
                err = "Устройство заблокировано: " + d["fault"]
            elif node == "Bath/WaterValve" and p.get("power") == "on" and DEV["Bath/LeakSensor"]["state"]["wet"]:
                err = "Датчик фиксирует протечку"
            if err:
                await send(type="ack", cmd_id=msg["cmd_id"], ok=False, error=err); continue
            d["state"].update(p)
            await send_state(node, cmd_id=msg["cmd_id"])
            receipt=dict(type="ack",cmd_id=msg["cmd_id"],ok=True)
            RECEIPTS[msg["cmd_id"]]=receipt
            await send(**receipt)

    tasks = [asyncio.create_task(sim()), asyncio.create_task(keyboard())]
    try:
        await commands()
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def read_console(loop, commands):
    for raw in sys.stdin:
        if loop.is_closed():
            return
        loop.call_soon_threadsafe(commands.put_nowait, raw.strip().lower())
    if not loop.is_closed():
        loop.call_soon_threadsafe(commands.put_nowait, None)


async def main(url):
    console_commands = asyncio.Queue()
    loop = asyncio.get_running_loop()
    Thread(target=read_console, args=(loop, console_commands), daemon=True).start()
    while True:
        try:
            async with connect(url) as ws:
                print(f"Подключён к {url}. Введите код ошибки, r для восстановления давления или c для сброса котла."
                      " l — протечка, d — сухо.", flush=True)
                await session(ws, console_commands)
        except (OSError, ConnectionClosed) as exc:
            print(f"Нет связи со шлюзом ({type(exc).__name__}), повтор через 2 с", flush=True)
        await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL))
    except KeyboardInterrupt:
        pass
