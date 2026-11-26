"""Эмулятор Godot: 6 устройств из config/devices.json, телеметрия раз в секунду, квитирование команд.
Консоль:  e + Enter = ошибка E04 на котле,  l + Enter = протечка, d + Enter = сухо, r + Enter = восстановить давление,  Ctrl+C = выход.
Запуск:   python tools/fake_godot.py [ws://127.0.0.1:8765]"""
import asyncio
import json
import random
import sys
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


async def session(ws, console_commands):
    async def send(**m): await ws.send(json.dumps(m))
    for node, d in DEV.items():                                   # полное состояние после подключения
        if d["state"]: await send(type="state", node=node, values=d["state"])
        if d.get("fault"): await send(type="error", node=node, code=d["fault"])

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
                await send(type="telemetry", node=node, values=values)
            await send(type="telemetry", node="Living/Thermometer",
                       values={"temperature": round(DEV["Living/AC"]["temp"], 1)})
            h = DEV["Bath/LeakSensor"]; h["humidity"] = max(30, min(95, h["humidity"] + random.uniform(-1, 1)))
            await send(type="telemetry", node="Bath/LeakSensor", values={"humidity": round(h["humidity"], 1)})
            for node in ("Bath/LeakSensor", "Bath/WaterValve", "Spare/Lamp"):
                await send(type="state", node=node, values=DEV[node]["state"])
            await asyncio.sleep(1)

    async def keyboard():
        while True:
            line = await console_commands.get()
            if line is None:
                return
            boiler = DEV["Kitchen/Boiler"]
            if line == "e":
                boiler.update(fault="E04", pressure=0.4)
                boiler["state"]["power"] = "off"
                await send(type="error", node="Kitchen/Boiler", code="E04", message="Недостаточное давление в контуре")
                await send(type="state", node="Kitchen/Boiler", values=boiler["state"])
            elif line == "r":
                boiler["pressure"] = 1.4
                await send(type="telemetry", node="Kitchen/Boiler", values={"pressure": boiler["pressure"]})
            elif line == "d":
                sensor = DEV["Bath/LeakSensor"]
                sensor["state"]["wet"] = False
                await send(type="state", node="Bath/LeakSensor", values=sensor["state"])
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
            elif node == "Kitchen/Boiler" and p.get("reset_error"):
                if d["pressure"] < 1.0:
                    err = "Давление в контуре ещё не восстановлено"
                else:
                    d["fault"] = ""
                    p = {}
            elif d.get("fault"):
                err = "Котёл заблокирован: " + d["fault"]
            if err:
                await send(type="ack", cmd_id=msg["cmd_id"], ok=False, error=err); continue
            d["state"].update(p)
            await send(type="state", node=node, cmd_id=msg["cmd_id"], values=d["state"])
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
                print(f"Подключён к {url}. e — E04; l — протечка; d — сухо; r — восстановить давление.", flush=True)
                await session(ws, console_commands)
        except (OSError, ConnectionClosed) as exc:
            print(f"Нет связи со шлюзом ({type(exc).__name__}), повтор через 2 с", flush=True)
        await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL))
    except KeyboardInterrupt:
        pass
