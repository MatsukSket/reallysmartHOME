"""Regression checks for read-only reference answers and index updates."""
import sys
from types import SimpleNamespace

import pytest

from test_storage_chat import local

from app.chat.service import Chat
from app.rag import indexer
from app.rag.corpus import CHUNK_SIZE, OVERLAP, parse_manual
from app.rag.retriever import Knowledge


def test_help_never_offers_a_command_for_another_device(local, monkeypatch):
    client, solver = local
    monkeypatch.setattr(solver, "execute", lambda *_: pytest.fail("Справочный запрос запустил команду"))
    reply = client.post("/api/chat", json={"message": "Котёл выдал E04 и дома душно, что делать?"}).raise_for_status().json()
    assert reply["kind"] == "info"
    assert "confirmation" not in reply
    assert {source.split("#")[0] for source in reply["sources"]} == {"boiler_baxi.md"}
    mixed = client.post("/api/chat", json={
        "message": "Котёл выдал E04. Предложи переключить кондиционер на вентиляцию.",
    }).raise_for_status().json()
    assert mixed["kind"] == "clarify" and "confirmation" not in mixed


def test_manual_for_another_model_is_marked_as_reference(local, monkeypatch):
    client, solver = local
    with solver._guard:
        device = solver.storage.device("boiler")
        device.model = "Другая модель"
        solver.storage.save_device(device)
    monkeypatch.setattr(solver, "execute", lambda *_: pytest.fail("Справочный запрос запустил команду"))
    reply = client.post("/api/chat", json={"message": "Что означает E04 у котла?"}).raise_for_status().json()
    assert "другой модели" in reply["reply"]
    assert reply["sources"] and "confirmation" not in reply


def test_false_claim_in_reference_answer_falls_back_to_sources(local, monkeypatch):
    _, solver = local
    chat = Chat(solver, "ollama", lambda result: result)
    chat.knowledge = Knowledge()
    session = SimpleNamespace(username="admin", role="admin")
    monkeypatch.setattr(chat.model, "explain", lambda *_: "Действие уже выполнено.")
    monkeypatch.setattr(solver, "execute", lambda *_: pytest.fail("Справочный запрос запустил команду"))
    reply = chat.answer("Что означает E04?", session)
    assert reply["kind"] == "degraded"
    assert "Действие уже выполнено" not in reply["reply"]
    assert "давление" in reply["reply"] and reply["sources"] and "confirmation" not in reply


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

def test_missing_manual_does_not_call_ollama(local, monkeypatch):
    _, solver = local
    chat = Chat(solver, "ollama", lambda result: result)
    chat.knowledge = Knowledge()
    session = SimpleNamespace(username="admin", role="admin")
    monkeypatch.setattr(chat.model, "explain", lambda *_: pytest.fail("Модель вызвана без источников"))
    reply = chat.answer("Что означает E99?", session)
    assert reply["kind"] == "no_source" and reply["sources"] == []
