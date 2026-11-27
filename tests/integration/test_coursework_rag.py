"""Regression tests for the coursework retrieval and grounded-action scenarios."""
import sys
from types import SimpleNamespace

import pytest

from test_storage_chat import local

from app.chat.service import Chat
from app.domain import CommandResult
from app.rag import indexer
from app.rag.corpus import CHUNK_SIZE, OVERLAP, parse_manual
from app.schemas.chat import GroundedCommand, GroundedSolution


QUESTION = "Котёл выдал E04 и дома душно, что делать?"


def prepare_scenario(solver):
    with solver._guard:
        for ident in ("boiler", "conditioner_living", "sensor_temperature_living"):
            device = solver.storage.device(ident)
            device.received_at = device.last_event_time = solver.clock.now()
            if ident == "boiler":
                device.error = "E04"
            if ident == "sensor_temperature_living":
                device.state["value"] = 27
            solver.storage.save_device(device)


def test_compound_question_finds_two_sources_and_waits_for_confirmation(local, monkeypatch):
    client, solver = local
    prepare_scenario(solver)
    operations = []

    def execute(command):
        operations.append(command.operation)
        return CommandResult("выполнено")

    monkeypatch.setattr(solver, "execute", execute)
    reply = client.post("/api/chat", json={"message": QUESTION}).raise_for_status().json()
    assert reply["kind"] == "confirm" and operations == []
    assert {source.split("#")[0] for source in reply["sources"]} == {"boiler_baxi.md", "conditioner_sim.md"}
    assert any(item["section"] for item in reply["source_details"])
    assert "27 °C" in reply["reply"]
    result = client.post("/api/chat/confirm/" + reply["confirmation"]).raise_for_status().json()
    assert operations == ["set_mode", "turn_on"]
    assert result["skipped"] == 0 and len(result["results"]) == 2
    assert client.post("/api/chat/confirm/" + reply["confirmation"]).status_code == 403


def test_stale_sensor_and_mismatched_model_block_proposals(local):
    client, solver = local
    prepare_scenario(solver)
    proposal = client.post("/api/chat", json={"message": QUESTION}).raise_for_status().json()
    assert proposal["kind"] == "confirm"
    with solver._guard:
        sensor = solver.storage.device("sensor_temperature_living")
        sensor.received_at = sensor.last_event_time = None
        solver.storage.save_device(sensor)
    assert client.post("/api/chat/confirm/" + proposal["confirmation"]).status_code == 409
    assert client.post("/api/chat", json={"message": QUESTION}).json()["kind"] == "info"

    prepare_scenario(solver)
    with solver._guard:
        device = solver.storage.device("conditioner_living")
        device.model = "Другая модель"
        solver.storage.save_device(device)
    reply = client.post("/api/chat", json={"message": QUESTION}).raise_for_status().json()
    assert "не подтверждена" in reply["reply"] and "confirmation" not in reply


def test_model_invented_operation_is_audited_and_never_sent(local, monkeypatch):
    _, solver = local
    prepare_scenario(solver)
    chat = Chat(solver, "demo", lambda result: result)
    chat.model = SimpleNamespace(synthesize=lambda payload: GroundedSolution(reply="Действие уже выполнено.", commands=[
        GroundedCommand(device_id="boiler", operation="reset_error", value=None, source_id="invented"),
    ]))
    session = SimpleNamespace(username="admin", role="admin")
    monkeypatch.setattr(solver, "execute", lambda *_: pytest.fail("Команда модели ушла на исполнение"))
    reply = chat.answer(QUESTION, session)
    assert "отклонено" in reply["reply"] and "confirmation" not in reply
    assert "Действие уже выполнено" not in reply["reply"]
    assert any(item["status"] == "отклонено" and item["reason"] == "reset_error"
               for item in solver.journal_records())


def test_index_keeps_error_metadata_overlap_and_previous_version(tmp_path, monkeypatch):
    raw = "---\ndevice_type: boiler\nmodel: Check-1\n---\n## Диагностика\nОшибка E04: " + "давление низкое " * 100
    chunks = parse_manual("checks.md", raw)
    assert len(chunks) > 1 and all(chunk["code"] == "E04" and chunk["section"] == "Диагностика" for chunk in chunks)
    assert all(len(chunk["text"]) <= CHUNK_SIZE for chunk in chunks)
    assert chunks[0]["text"][-(OVERLAP - 1):] in chunks[1]["text"][:OVERLAP + 3]

    root = tmp_path / "rag"
    old = root / "prior"
    old.mkdir(parents=True)
    (root / "CURRENT").write_text("prior", encoding="ascii")
    monkeypatch.setattr(indexer, "ROOT", root)
    with pytest.raises(ValueError):
        indexer.create_index(("bad.md", "---\nwrong metadata"))
    assert indexer.active_version() == old
    monkeypatch.setitem(sys.modules, "chromadb", SimpleNamespace(
        PersistentClient=lambda **_: SimpleNamespace(create_collection=lambda *args, **kwargs: SimpleNamespace(add=lambda **_: None))))
    monkeypatch.setattr(indexer, "encoder", lambda: SimpleNamespace(
        encode=lambda *args, **kwargs: SimpleNamespace(tolist=lambda: [[1.0]])))
    version = indexer.create_index(("checks.md", raw))
    assert indexer.active_version().name == version and old.is_dir()
    assert any(chunk["source"] == "checks.md" for chunk in indexer.passages())

def test_synthesis_retries_invalid_json_and_rejects_extra_fields(monkeypatch):
    import httpx
    from app.rag.assistant import Ollama

    requests = []
    contents = iter(['{"reply":"invalid","commands":[],"extra":1}', '{"reply":"Готово","commands":[]}'])

    def post(url, **kwargs):
        requests.append(kwargs["json"]["messages"][0]["content"])
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"message": {"content": next(contents)}})

    monkeypatch.setattr(httpx, "post", post)
    assert Ollama().synthesize({"allowed": []}).commands == []
    assert len(requests) == 2 and "нарушил схему" in requests[-1]
