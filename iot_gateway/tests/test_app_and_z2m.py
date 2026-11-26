import asyncio
import json
from pathlib import Path

import httpx

from gateway.adapters.zigbee2mqtt import Zigbee2MqttAdapter
from gateway.app import create_app, load_settings
from gateway.models import Command, CommandStatus
from gateway.registry import Registry

CFG = Path(__file__).parent.parent / "config" / "devices.json"


async def test_http_commands_endpoint_validation_and_unavailable():
    settings = {**load_settings(), "adapter": "godot", "godot_host": "127.0.0.1", "godot_port": 0,
                "registry_path": str(CFG), "ack_timeout": 0.3}

    async def sender(ev): ...

    app = create_app(settings, sender=sender)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/commands", json={"commands": [
                {"device_id": 123, "params": {"target_temp": 500}},      # вне диапазона -> rejected
                {"device_id": 123, "params": {"power": "on"}},           # Godot не подключён -> unavailable
            ]})
            res = r.json()["results"]
            assert [x["status"] for x in res] == ["rejected", "unavailable"]
            assert (await c.get("/health")).json() == {"adapter": "godot", "transport_connected": False}
            assert {d["id"] for d in (await c.get("/devices")).json()} == {123, 124, 125, 126, 127, 128}


async def test_conflicting_batch_is_rejected_without_dispatch():
    settings = {**load_settings(), "adapter": "godot", "godot_port": 0, "registry_path": str(CFG)}
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        called = []

        async def execute(cmd):
            called.append(cmd)
            raise AssertionError("Пакет с конфликтом не должен выполняться")

        app.state.adapter.execute = execute
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/commands", json={"commands": [
                {"cmd_id": "same", "device_id": 123, "params": {"power": "on"}},
                {"cmd_id": "same", "device_id": 123, "params": {"power": "off"}},
            ]})
        assert response.status_code == 409
        assert called == []


class FakeMqtt:
    def __init__(self, adapter, reply=True):
        self.adapter, self.reply, self.published = adapter, reply, []

    async def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))
        if self.reply:       # «устройство» присылает новое состояние
            asyncio.create_task(self.adapter._on_message(
                "zigbee2mqtt/boiler", json.dumps({"state": "ON", "current_heating_setpoint": 60, "linkquality": 90})))


async def test_z2m_confirm_by_state_and_timeout():
    reg = Registry.load(CFG, "zigbee2mqtt")
    events = []

    async def emit(ev): events.append(ev)

    ad = Zigbee2MqttAdapter(reg, emit, ack_timeout=0.3)
    ad._client = FakeMqtt(ad, reply=True)
    res = await ad.execute(Command(device_id=123, params={"power": "on", "target_temp": 60}))
    assert res.status is CommandStatus.CONFIRMED
    assert ad._client.published == [("zigbee2mqtt/boiler/set", {"state": "ON", "current_heating_setpoint": 60})]
    assert any(e.kind.value == "state" for e in events)

    ad._client = FakeMqtt(ad, reply=False)
    res = await ad.execute(Command(device_id=123, params={"power": "on"}))
    assert res.status is CommandStatus.TIMEOUT
    assert ad._expect == {}

    await ad._on_message("zigbee2mqtt/boiler/availability", json.dumps({"state": "offline"}))
    assert events[-1].payload["code"] == "DEVICE_OFFLINE"


async def test_demo_hub_collects_events_and_serves_snapshot(monkeypatch):
    monkeypatch.setenv("GATEWAY_TOKEN", "test-token")
    settings = {**load_settings(), "adapter": "godot", "godot_host": "127.0.0.1", "godot_port": 0,
                "registry_path": str(CFG), "demo_ui": True}

    async def sender(ev): ...

    app = create_app(settings, sender=sender)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            ev = {"device_id": 123, "kind": "error", "payload": {"code": "E04"}, "source": "godot"}
            assert (await c.post("/api/gateway/events", json=ev)).status_code == 200
            snap = (await c.get("/api/demo/snapshot")).json()
            assert snap["devices"]["123"]["last_error"]["code"] == "E04"
            assert snap["read_only"]
            await c.post("/api/gateway/events", json={**ev, "kind": "discovery", "device_id": 1999,
                                                     "payload": {"type": "light", "name": "Лампа"}})
            snap = (await c.get("/api/demo/snapshot")).json()
            assert snap["devices"]["1999"]["last_error"] is None
            await c.post("/api/gateway/events", json={**ev, "kind": "removal", "payload": {}})
            await c.post("/api/gateway/events", json=ev)
            snap = (await c.get("/api/demo/snapshot")).json()
            assert snap["retired"] == [123] and "123" not in snap["devices"]
            assert "<title>IoT Gateway" in (await c.get("/ui")).text
