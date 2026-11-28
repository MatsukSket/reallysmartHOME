"""Сквозные проверки в отдельной временной базе и на свободных локальных портах."""
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]

def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1",0))
        return s.getsockname()[1]

def until(fn,seconds=20):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            v=fn()
            if v: return v
        except httpx.TransportError: pass
        time.sleep(.1)
    raise AssertionError("Истекло ожидание "+repr(fn))

@pytest.fixture
def house(tmp_path):
    backend,gateway,ws=port(),port(),port()
    url=f"http://127.0.0.1:{backend}"
    env=dict(os.environ,HOME_DB=str(tmp_path/"demo.sqlite3"),STORAGE="sqlite",ASSISTANT=os.getenv("CHECK_ASSISTANT","demo"),
             HOME_MODEL="egor",GATEWAY_TOKEN=secrets.token_urlsafe(),GATEWAY_ADAPTER="godot",
             GODOT_PORT=str(ws),GODOT_HOST="127.0.0.1",GATEWAY_URL=f"http://127.0.0.1:{gateway}",
             BACKEND_EVENTS_URL=url+"/api/gateway/events",PYTHONUTF8="1",ADMIN_PASSWORD="demo-test",
             SIM_INVENTORY_PATH=str(tmp_path/"godot-inventory.cfg"))
    jobs=[]
    def start(folder,args):
        p=subprocess.Popen([os.environ.get("DEMO_PYTHON",sys.executable),*args],cwd=ROOT/folder,env=env,stdin=subprocess.PIPE)
        jobs.append(p)
        return p
    def server():
        p=start("backend",["-m","uvicorn","app.main:app","--port",str(backend)])
        until(lambda:httpx.get(url+"/api/health").is_success)
        return p
    try:
        b=server()
        start("iot_gateway",["-m","uvicorn","gateway.app:app","--port",str(gateway)])
        if os.getenv("SCENE_EXE"):
            scene = ["res://tests/catalog_check.tscn"] if os.getenv("CHECK_GODOT_CATALOG") else []
            options = ["--test-e04"]
            if os.getenv("CHECK_GODOT_TELEMETRY"):
                options.append("--test-telemetry")
            if os.getenv("CHECK_GODOT_REMOVAL"):
                options.append("--test-remove-lamp")
            fake=subprocess.Popen([os.environ["SCENE_EXE"],"--position","720,40","--path",str(ROOT/"godot_sim"),
                                   *scene, "--", *options],env=env)
            jobs.append(fake)
        else:
            fake=start("iot_gateway",["tools/fake_godot.py",f"ws://127.0.0.1:{ws}"])
        with httpx.Client(base_url=url,timeout=15) as client:
            yield client,fake,b,server
    finally:
        for p in jobs:
            if p.poll() is None: p.terminate()
        for p in jobs: p.wait(timeout=10)

def test_loop(house):
    c,fake,backend,restart=house
    def post(path,**data):
        r=c.post("/api/"+path,json=data)
        r.raise_for_status()
        return r.json()
    credentials=post("auth/login",username="admin",password="demo-test")
    c.headers["Authorization"]="Bearer "+credentials["access_token"]
    def device(ident):
        return next(d for r in c.get("/api/home").json()["rooms"] for d in r["devices"] if d["id"]==ident)
    ac="conditioner_living"
    until(lambda:device(ac)["updated_at"])
    until(lambda:device("boiler")["updated_at"])
    setpoint = post("chat", message="Выставь котлу на кухне 60 градусов")
    assert setpoint["result"]["status"] == "выполнено"
    assert device("boiler")["state"]["coolant_temperature"] == 60

    result=post("commands",device_id=ac,operation="turn_on")
    assert result["status"]=="выполнено",result
    assert device(ac)["state"]["power"] is True
    assert post("chat",message="включи")["kind"]=="clarify"
    until(lambda:device("leak_protection")["updated_at"])
    confirmation=post("chat",message="перекрой воду")["confirmation"]
    assert device("leak_protection")["state"]["water"]=="open"
    assert post("chat/confirm/"+confirmation)["status"]=="выполнено"
    assert c.post("/api/chat/confirm/"+confirmation).status_code==403
    reopened=post("chat",message="открой воду")["confirmation"]
    assert post("chat/confirm/"+reopened)["status"]=="выполнено"
    fake.stdin.write(b"e\nl\n");fake.stdin.flush()
    until(lambda:device("boiler")["error"]=="E04")
    until(lambda:device("sensor_leak_bathroom")["state"]["value"] is True)
    until(lambda:device("leak_protection")["state"]["water"]=="closed")
    assert post("commands",device_id="leak_protection",operation="open_water")["status"]=="отклонено"
    answer=post("chat",message="Что означает E04?")
    assert any("boiler_baxi" in s for s in answer["sources"])
    assert c.get("/api/journal").json()["records"]
    post("targets",room_id="living",parameter="temperature",value=28,device_id=ac)
    schedule=post("schedules/command",device_id="boiler",operation="turn_off",at="05:25")
    assert device(ac)["lock"] and device(ac)["state"]["power"]
    backend.terminate();backend.wait();restart()
    new=post("auth/refresh",refresh_token=credentials["refresh_token"])
    c.headers["Authorization"]="Bearer "+new["access_token"]
    assert device(ac)["state"]["power"] and device("boiler")["error"]=="E04"
    assert device(ac)["lock"]
    assert schedule["id"] in [s["id"] for s in c.get("/api/home").json()["schedules"]]
    post("devices/"+ac+"/release-lock")
    until(lambda:device(ac)["state"]["power"] is False)
    fake.terminate();fake.wait()
    failure=post("commands",device_id=ac,operation="turn_on")
    assert failure["status"]=="не подтверждено" and not device(ac)["state"]["power"]
    assert c.get("/api/journal").json()["records"]

def test_read_state_command_and_manual_are_independent(house):
    client, fake, _, _ = house
    if not fake.stdin:
        pytest.skip("Сценарий запускаемой ошибки для этого теста предусмотрен в эмуляторе")

    def post(path, **data):
        return client.post("/api/" + path, json=data).raise_for_status().json()

    def device(ident):
        return next(d for room in client.get("/api/home").json()["rooms"] for d in room["devices"] if d["id"] == ident)

    token = post("auth/login", username="admin", password="demo-test")["access_token"]
    client.headers["Authorization"] = "Bearer " + token
    until(lambda: device("sensor_temperature_living")["updated_at"])
    until(lambda: device("conditioner_living")["updated_at"])
    state = post("chat", message="Какая температура в гостиной?")
    assert state["kind"] == "info" and "°C" in state["reply"] and not state["sources"]

    fake.stdin.write(b"e\n")
    fake.stdin.flush()
    until(lambda: device("boiler")["error"] == "E04")
    help_reply = post("chat", message="Что означает E04?")
    assert help_reply["kind"] == "info" and "confirmation" not in help_reply
    assert {source.split("#")[0] for source in help_reply["sources"]} == {"boiler_baxi.md"}
    assert device("conditioner_living")["state"]["power"] is False

    action = post("chat", message="Включи кондиционер в гостиной")
    assert action["kind"] == "action" and action["result"]["status"] == "выполнено"
    assert not action["sources"] and device("conditioner_living")["state"]["power"] is True
    assert device("boiler")["error"] == "E04"


def test_simulated_faults_use_one_manual_per_device_and_clear_only_locally(house):
    client, simulator, _, _ = house
    if not simulator.stdin:
        pytest.skip("Для ввода кодов используется консольный эмулятор")

    def post(path, **data):
        return client.post("/api/" + path, json=data).raise_for_status().json()

    def snapshot():
        return client.get("/api/home").raise_for_status().json()

    def device(ident):
        return next(item for room in snapshot()["rooms"] for item in room["devices"] if item["id"] == ident)

    def simulate(text):
        simulator.stdin.write((text + "\n").encode())
        simulator.stdin.flush()

    token = post("auth/login", username="admin", password="demo-test")["access_token"]
    client.headers["Authorization"] = "Bearer " + token
    client.put("/api/devices/light_spare/connection", json={"room_id": "living"}).raise_for_status()

    cases = [
        ("boiler", "boiler_baxi.md", "clear boiler", "turn_on", ("E04", "E10")),
        ("conditioner_living", "conditioner_sim.md", "clear ac", "turn_on", ("E11", "E12")),
        ("leak_protection", "water_valve_sim.md", "clear valve", "open_water", ("E21", "E22")),
        ("light_spare", "lamp_sim.md", "clear lamp", "turn_on", ("E31", "E32")),
    ]
    for ident, source, reset, operation, codes in cases:
        for code in codes:
            simulate(code)
            until(lambda: device(ident)["error"] == code)
            denied = post("commands", device_id=ident, operation=operation)
            assert denied["status"] == "отклонено" and code in denied["reason"]
            answer = post("chat", message=f"Что означает {code}?")
            assert {item.split("#")[0] for item in answer["sources"]} == {source}
            assert "confirmation" not in answer
            if code == "E04":
                assert "только в симуляторе" in post("commands", device_id=ident, operation="reset_error")["reason"]
                until(lambda: device(ident)["state"].get("pressure") == 0.4)
                simulate("r")
                until(lambda: device(ident)["state"].get("pressure") == 1.4)
                assert device(ident)["error"] == code
            if code == "E22":
                assert post("commands", device_id=ident, operation="close_water")["status"] == "выполнено"
            simulate(reset)
            until(lambda: device(ident)["error"] is None)
            assert any(event["kind"] == "error_cleared" and event["code"] == code
                       for event in snapshot()["history"])
