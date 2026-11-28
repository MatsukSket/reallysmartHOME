import asyncio
import json
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.client import connect

from gateway.adapters.godot import GodotAdapter
from gateway.adapters.zigbee2mqtt import from_z2m, to_z2m
from gateway.models import Command, CommandStatus, DeviceEvent, EventKind
from gateway.pipeline import BackendForwarder, EventPipeline
from gateway.registry import Registry

CFG = Path(__file__).parent.parent / "config" / "devices.json"


class Env:
    """Шлюз целиком (без HTTP): Godot-адаптер + конвейер + «бэкенд», собирающий события."""
    def __init__(self, ack_timeout=0.4, backend_ok=True):
        self.received: list[DeviceEvent] = []
        self.backend_ok = backend_ok
        self.registry = Registry.load(CFG, "godot")

        async def sender(ev):
            if not self.backend_ok:
                raise ConnectionError("backend down")
            self.received.append(ev)

        self.fwd = BackendForwarder(sender)
        self.adapter = GodotAdapter(self.registry, EventPipeline(self.registry, self.fwd),
                                    host="127.0.0.1", port=0, ack_timeout=ack_timeout)

    async def __aenter__(self):
        await self.fwd.start()
        await self.adapter.start()
        return self

    async def __aexit__(self, *a):
        await self.adapter.stop()
        await self.fwd.stop()

    async def godot(self):
        ws = await connect(f"ws://127.0.0.1:{self.adapter.port}")
        for _ in range(50):
            if self.adapter.connected:
                break
            await asyncio.sleep(0.01)
        return ws

    async def wait_events(self, n, timeout=2.0):
        for _ in range(int(timeout / 0.01)):
            if len(self.received) >= n:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"ожидалось {n} событий, получено {len(self.received)}")


async def test_error_normalized_to_canonical_id():
    async with Env() as env:
        ws = await env.godot()
        await ws.send(json.dumps({"type": "error", "node": "Kitchen/Boiler", "code": "E04", "msg_id": 7}))
        assert json.loads(await ws.recv()) == {"type": "received", "msg_id": 7}
        await env.wait_events(1)
        ev = env.received[0]
        assert (ev.device_id, ev.kind, ev.payload["code"], ev.source) == (123, EventKind.ERROR, "E04", "godot")
        await ws.close()


async def test_impossible_and_duplicate_telemetry_filtered():
    async with Env() as env:
        ws = await env.godot()
        for v in (65.0, 65.0, 65.01, 9999.0, 70.0):     # норма, дубль, в мёртвой зоне, невозможное, новое
            await ws.send(json.dumps({"type": "telemetry", "node": "Kitchen/Boiler", "values": {"temperature": v}}))
        await env.wait_events(2)
        await asyncio.sleep(0.2)
        assert [e.payload["temperature"] for e in env.received] == [65.0, 70.0]
        await ws.close()


async def test_unknown_node_and_garbage_ignored():
    async with Env() as env:
        ws = await env.godot()
        await ws.send("not json")
        await ws.send(json.dumps({"type": "telemetry", "node": "Nowhere/X", "values": {"a": 1}}))
        await ws.send(json.dumps({"type": "error", "node": "Kitchen/Boiler", "code": "E04"}))
        await env.wait_events(1)
        assert len(env.received) == 1
        await ws.close()


async def test_command_confirmed():
    async with Env() as env:
        ws = await env.godot()

        async def fake_godot():
            msg = json.loads(await ws.recv())
            assert msg["node"] == "Kitchen/Boiler" and msg["params"] == {"power": "on", "target_temp": 60}
            await ws.send(json.dumps({"type": "ack", "cmd_id": msg["cmd_id"], "ok": True}))

        t = asyncio.create_task(fake_godot())
        res = await env.adapter.execute(Command(device_id=123, params={"power": "on", "target_temp": 60}))
        await t
        assert res.status is CommandStatus.CONFIRMED
        await ws.close()


async def test_command_rejected_by_device():
    async with Env() as env:
        ws = await env.godot()

        async def fake_godot():
            msg = json.loads(await ws.recv())
            await ws.send(json.dumps({"type": "ack", "cmd_id": msg["cmd_id"], "ok": False, "error": "заблокировано"}))

        t = asyncio.create_task(fake_godot())
        res = await env.adapter.execute(Command(device_id=123, params={"power": "on"}))
        await t
        assert (res.status, res.detail) == (CommandStatus.REJECTED, "заблокировано")
        await ws.close()


async def test_command_timeout_without_ack():
    async with Env(ack_timeout=0.2) as env:
        ws = await env.godot()
        res = await env.adapter.execute(Command(device_id=123, params={"power": "on"}))
        assert res.status is CommandStatus.TIMEOUT
        await ws.close()


async def test_unavailable_when_godot_not_connected():
    async with Env() as env:
        res = await env.adapter.execute(Command(device_id=123, params={"power": "on"}))
        assert res.status is CommandStatus.UNAVAILABLE


async def test_disconnect_fails_pending_command_fast():
    async with Env(ack_timeout=5) as env:
        ws = await env.godot()
        task = asyncio.create_task(env.adapter.execute(Command(device_id=123, params={"power": "on"})))
        await ws.recv()
        await ws.close()
        res = await asyncio.wait_for(task, 1.0)
        assert res.status is CommandStatus.UNAVAILABLE


def test_registry_validation_boundaries():
    reg = Registry.load(CFG, "godot")
    assert reg.validate_command(Command(device_id=123, params={"target_temp": 80})) is None      # граница OK
    assert "максимума" in reg.validate_command(Command(device_id=123, params={"target_temp": 81}))
    assert "не поддерж" in reg.validate_command(Command(device_id=123, params={"color": "red"}))
    assert "не зарегистр" in reg.validate_command(Command(device_id=999, params={"power": "on"}))
    assert "допустимые" in reg.validate_command(Command(device_id=123, params={"power": "maybe"}))


async def test_errors_survive_backend_outage():
    """Критично: аварии не теряются, пока бэкенд недоступен, и доходят после восстановления."""
    async with Env() as env:
        env.backend_ok = False
        ws = await env.godot()
        await ws.send(json.dumps({"type": "error", "node": "Kitchen/Boiler", "code": "E04"}))
        await asyncio.sleep(0.5)
        assert env.received == []
        env.backend_ok = True
        await env.wait_events(1, timeout=3)
        assert env.received[0].payload["code"] == "E04"
        await ws.close()


async def test_fault_and_clear_in_state_survive_backend_outage():
    async with Env() as env:
        env.backend_ok = False
        ws = await env.godot()
        for fault in ("E11", "E11", None):
            await ws.send(json.dumps({
                "type": "state", "node": "Living/AC", "values": {"power": "off", "fault": fault},
            }))
        await asyncio.sleep(0.5)
        assert env.received == []
        env.backend_ok = True
        await env.wait_events(2, timeout=3)
        assert [event.payload["fault"] for event in env.received] == ["E11", None]
        await ws.close()


async def test_invalid_critical_event_does_not_block_next_event():
    received = asyncio.Event()

    async def send(event):
        if event.payload["code"] == "invalid":
            request = httpx.Request("POST", "http://backend/events")
            httpx.Response(422, request=request).raise_for_status()
        received.set()

    forwarder = BackendForwarder(send)
    await forwarder.start()
    try:
        forwarder.submit(DeviceEvent(device_id=123, kind=EventKind.ERROR, source="godot",
                                     payload={"code": "invalid"}))
        forwarder.submit(DeviceEvent(device_id=123, kind=EventKind.ERROR, source="godot",
                                     payload={"code": "E04"}))
        await asyncio.wait_for(received.wait(), 1)
    finally:
        await forwarder.stop()


# ---- перевод Zigbee2MQTT (чистые функции, брокер не нужен) ----
def test_z2m_translation_roundtrip():
    reg = Registry.load(CFG, "zigbee2mqtt")
    ac = reg.get(124)
    assert to_z2m(ac, {"power": "on", "target_temp": 22, "mode": "cool"}) == \
        {"state": "ON", "occupied_cooling_setpoint": 22, "system_mode": "cool"}
    state, telem, alarms = from_z2m(ac, {"state": "ON", "occupied_cooling_setpoint": 22,
                                         "temperature": 24.5, "linkquality": 80})
    assert state == {"power": "on", "target_temp": 22} and telem == {"temperature": 24.5} and alarms == []
    _, _, alarms = from_z2m(reg.get(126), {"water_leak": True, "battery": 90})
    assert alarms == ["WATER_LEAK"]
