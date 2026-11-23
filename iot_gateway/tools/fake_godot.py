"""Эмулятор Godot: 4 устройства из config/devices.json, телеметрия раз в секунду, квитирование команд.
Консоль:  e + Enter = ошибка E04 на котле,  l + Enter = протечка,  Ctrl+C = выход.
Запуск:   python tools/fake_godot.py [ws://127.0.0.1:8765]"""
import asyncio, json, random, sys
from websockets.asyncio.client import connect

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8765"
DEV = {
    "Kitchen/Boiler":  {"state": {"power": "off", "target_temp": 50.0}, "temp": 20.0},
    "Living/AC":       {"state": {"power": "off", "target_temp": 22.0, "mode": "auto"}, "temp": 25.0},
    "Bath/WaterValve": {"state": {"power": "on"}},
    "Bath/LeakSensor": {"state": {}, "humidity": 40.0},
}
RULES = {"Kitchen/Boiler": (30, 80), "Living/AC": (16, 30)}


async def session(ws):
    async def send(**m): await ws.send(json.dumps(m))
    for node, d in DEV.items():                                   # полное состояние после подключения
        if d["state"]: await send(type="state", node=node, values=d["state"])

    async def sim():
        while True:
            for node in ("Kitchen/Boiler", "Living/AC"):
                d, s = DEV[node], DEV[node]["state"]
                goal = s["target_temp"] if s["power"] == "on" else (20.0 if "Boiler" in node else 25.0)
                d["temp"] += max(-0.8, min(0.8, goal - d["temp"])) * 0.5 + random.uniform(-0.05, 0.05)
                await send(type="telemetry", node=node, values={"temperature": round(d["temp"], 1)})
            h = DEV["Bath/LeakSensor"]; h["humidity"] = max(30, min(95, h["humidity"] + random.uniform(-1, 1)))
            await send(type="telemetry", node="Bath/LeakSensor", values={"humidity": round(h["humidity"], 1)})
            await asyncio.sleep(1)

    async def keyboard():
        loop = asyncio.get_running_loop()
        while True:
            line = (await loop.run_in_executor(None, sys.stdin.readline)).strip().lower()
            if line == "e":
                await send(type="error", node="Kitchen/Boiler", code="E04", message="Недостаточное давление в контуре")
            elif line == "l":
                await send(type="error", node="Bath/LeakSensor", code="WATER_LEAK", message="Обнаружена протечка")

    async def commands():
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "command": continue
            node, p = msg["node"], msg.get("params", {})
            print("← команда:", node, p)
            d = DEV.get(node); err = None
            if d is None: err = "узел не найден"
            elif "target_temp" in p and not (RULES[node][0] <= p["target_temp"] <= RULES[node][1]): err = "уставка вне диапазона"
            if err:
                await send(type="ack", cmd_id=msg["cmd_id"], ok=False, error=err); continue
            d["state"].update(p)
            await send(type="state", node=node, values=d["state"])
            await send(type="ack", cmd_id=msg["cmd_id"], ok=True)

    await asyncio.gather(sim(), keyboard(), commands())


async def main():
    while True:
        try:
            async with connect(URL) as ws:
                print(f"подключён к {URL}.  e+Enter = ошибка E04,  l+Enter = протечка")
                await session(ws)
        except (OSError, Exception) as exc:
            print(f"нет связи со шлюзом ({exc.__class__.__name__}), повтор через 2 с")
            await asyncio.sleep(2)

try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
