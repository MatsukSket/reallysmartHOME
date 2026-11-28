import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"backend"))
from app.api import create_app
from app.rag.assistant import Ollama

@pytest.fixture
def local(tmp_path,monkeypatch):
    for k,v in dict(HOME_MODEL="egor",GATEWAY_TOKEN="service-test",HOME_DB=str(tmp_path/"home.sqlite3"),
                    STORAGE="sqlite",ADMIN_PASSWORD="secret-test",ASSISTANT="demo").items():
        monkeypatch.setenv(k,v)
    app=create_app()
    with TestClient(app) as c:
        first=c.post("/api/auth/login",json=dict(username="admin",password="secret-test")).json()
        c.headers["Authorization"]="Bearer "+first["access_token"]
        yield c,app.state.solver

def observation(c,id,ts,wet):
    return c.post("/api/gateway/events",headers={"X-Gateway-Token":"service-test"},
        json=dict(event_id=id,source="godot",device_id=126,kind="state",ts=ts,payload={"wet":wet}))

def test_order_and_migration(local):
    c,s=local
    now=time.time()
    assert observation(c,"fresh",now,False).json()["accepted"]
    assert not observation(c,"fresh",now,False).json()["accepted"]
    assert not observation(c,"old",now-5,True).json()["accepted"]
    assert s.storage.device("sensor_leak_bathroom").state["value"] is False
    assert len(s.storage.read_history())==2
    with TestClient(create_app()) as second:
        assert not observation(second,"fresh",now,False).json()["accepted"]

def test_invites_sessions_and_foreign_confirmation(local):
    c,s=local
    assert c.post("/api/admin/invites",json=dict(username="demo")).status_code==403
    c.post("/api/auth/admin-mode",json=dict(password="secret-test")).raise_for_status()
    code=c.post("/api/admin/invites",json=dict(username="demo")).json()["code"]
    with TestClient(create_app()) as b:
        b.post("/api/auth/invite/accept",json=dict(code=code,password="resident-test")).raise_for_status()
        grant=b.post("/api/auth/login",json=dict(username="demo",password="resident-test")).json()
        b.headers["Authorization"]="Bearer "+grant["access_token"]
        assert b.get("/api/admin/users").status_code==403
        assert b.post("/api/rooms", json={"name": "Гараж"}).status_code == 403
        assert b.post("/api/knowledge/manuals", json={"filename": "private.md", "text": "Текст"}).status_code == 403
        assert b.put("/api/devices/boiler/connection", json={"room_id": None}).status_code == 403
        assert b.post("/api/auth/invite/accept",json=dict(code=code,password="again")).status_code==400
        admin=b.post("/api/auth/login",json=dict(username="admin",password="secret-test")).json()
        h={"Authorization":"Bearer "+admin["access_token"]}
        def valve(ts,id):
            return b.post("/api/gateway/events",headers={"X-Gateway-Token":"service-test"},json=dict(
                event_id=id,source="godot",device_id=125,kind="state",ts=ts,payload={"power":"on"}))
        valve(time.time()-600,"stale")
        assert b.post("/api/chat",json=dict(message="перекрой воду")).json()["kind"]=="degraded"
        valve(time.time(),"new")
        action=b.post("/api/chat",headers=h,json=dict(message="перекрой воду")).json()["confirmation"]
        assert b.post("/api/chat/confirm/"+action).status_code==403
        b.post("/api/auth/logout",json={}).raise_for_status()
        assert b.post("/api/auth/refresh",json=dict(refresh_token=grant["refresh_token"])).status_code==401

def test_ollama_http_schema(monkeypatch):
    import httpx
    seen=[]
    outputs = iter(['{"reply":"Справка по E04"}', '{"reply":"Справка","commands":[]}'])
    def post(url,**kw):
        seen.append(kw["json"])
        return httpx.Response(200,request=httpx.Request("POST",url),json={"message":{"content": next(outputs)}})
    monkeypatch.setattr(httpx,"post",post)
    assert "E04" in Ollama().explain("Что означает E04?", [])
    assert set(seen[0]["format"]["properties"]) == {"reply"}
    with pytest.raises(ValueError):
        Ollama().explain("Что означает E04?", [])


def test_catalog_persists_and_disconnected_devices_reject_commands(local):
    client, _ = local
    room = client.post("/api/rooms", json={"name": "Кабинет"}).raise_for_status().json()
    lamp = "/api/devices/light_spare/connection"
    assert client.put(lamp, json={"room_id": room["id"]}).raise_for_status().json()["connected"]
    assert client.delete(f'/api/rooms/{room["id"]}').status_code == 409
    with TestClient(create_app()) as restarted:
        grant = restarted.post("/api/auth/login", json={"username": "admin", "password": "secret-test"}).json()
        restarted.headers["Authorization"] = "Bearer " + grant["access_token"]
        snapshot = restarted.get("/api/home").raise_for_status().json()
        office = next(item for item in snapshot["rooms"] if item["id"] == room["id"])
        assert office["devices"][0]["id"] == "light_spare"
        assert not restarted.put(lamp, json={"room_id": None}).raise_for_status().json()["connected"]
        response = restarted.post("/api/commands", json={"device_id": "light_spare", "operation": "turn_on"}).json()
        assert response["status"] == "отклонено" and "отключено" in response["reason"]
        assert restarted.delete(f'/api/rooms/{room["id"]}').raise_for_status().json()["removed"]


def test_simulator_removal_is_idempotent_and_survives_restart(local):
    client, _ = local
    service_header = {"X-Gateway-Token": "service-test"}
    removal = dict(event_id="remove-spare", source="godot", device_id=127,
                   kind="removal", ts=time.time(), payload={})
    for _ in range(2):
        assert client.post("/api/gateway/events", headers=service_header, json=removal).raise_for_status().json()["accepted"]

    with TestClient(create_app()) as restarted:
        grant = restarted.post("/api/auth/login", json={"username": "admin", "password": "secret-test"}).json()
        restarted.headers["Authorization"] = "Bearer " + grant["access_token"]
        available = restarted.get("/api/catalog/available").raise_for_status().json()
        assert all(device["id"] != "light_spare" for device in available["devices"])
        assert restarted.put("/api/devices/light_spare/connection", json={"room_id": "living"}).status_code == 404
        late_state = {**removal, "event_id": "late-spare", "kind": "state",
                      "payload": {"power": "on", "brightness": 50}}
        assert not restarted.post("/api/gateway/events", headers=service_header, json=late_state).raise_for_status().json()["accepted"]
