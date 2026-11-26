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
