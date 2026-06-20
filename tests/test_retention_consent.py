"""Session retention/deletion and consent controls."""

from importlib import import_module

import httpx
import pytest
from httpx import ASGITransport

from archolith_proxy.compliance import apply_session_consent
from archolith_proxy.config import reset_settings
from archolith_proxy.config.constants import SESSION_CONFIG_DENYLIST
from archolith_proxy.main import create_app
from archolith_proxy.models.dtos import BackgroundPassTrace, TurnTrace
from archolith_proxy.trace.store import TraceStore


class FakeGraphBackend:
    def __init__(self) -> None:
        self.session_present = True
        self.active_facts = 2
        self.cached_files = [{"path": "a.py"}, {"path": "b.py"}]
        self.deleted = False

    async def find_session_by_id(self, session_id: str) -> dict | None:
        return {"session_id": session_id} if self.session_present else None

    async def get_active_fact_count(self, session_id: str) -> int:
        return self.active_facts

    async def list_cached_files(self, session_id: str) -> list[dict]:
        return self.cached_files

    async def delete_session_data(self, session_id: str) -> dict:
        self.session_present = False
        self.active_facts = 0
        self.cached_files = []
        self.deleted = True
        return {"nodes_deleted": 3}


class FakeTraceStore:
    def __init__(self) -> None:
        self.present = True
        self.deleted = False

    async def session_storage_summary(self, session_id: str) -> dict:
        return {
            "present": self.present,
            "turns": 1 if self.present else 0,
            "background_passes": 1 if self.present else 0,
            "metadata_keys": ["proxy_config"] if self.present else [],
            "jsonl_file": "",
            "jsonl_file_exists": False,
            "jsonl_file_bytes": 0,
        }

    async def delete_session_data(self, session_id: str) -> dict:
        self.present = False
        self.deleted = True
        return {
            "turns_deleted": 1,
            "background_passes_deleted": 1,
            "metadata_deleted": True,
            "jsonl_file_deleted": False,
        }


@pytest.mark.asyncio
async def test_admin_stored_endpoint_enumerates_graph_and_trace(monkeypatch):
    graph = FakeGraphBackend()
    trace = FakeTraceStore()
    admin_router = import_module("archolith_proxy.routers.admin_router")
    monkeypatch.setattr(admin_router, "is_graph_ready", lambda: True)
    monkeypatch.setattr(admin_router, "get_backend", lambda: graph)
    monkeypatch.setattr(admin_router, "get_trace_store", lambda: trace)

    app = create_app()
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/sessions/s1/stored")

    assert response.status_code == 200
    data = response.json()
    assert data["present"] is True
    assert data["stores"]["graph"]["active_facts"] == 2
    assert data["stores"]["graph"]["cached_files"] == 2
    assert data["stores"]["trace"]["turns"] == 1


@pytest.mark.asyncio
async def test_admin_delete_session_clears_graph_and_trace(monkeypatch):
    graph = FakeGraphBackend()
    trace = FakeTraceStore()
    admin_router = import_module("archolith_proxy.routers.admin_router")
    monkeypatch.setattr(admin_router, "is_graph_ready", lambda: True)
    monkeypatch.setattr(admin_router, "get_backend", lambda: graph)
    monkeypatch.setattr(admin_router, "get_trace_store", lambda: trace)

    app = create_app()
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete("/admin/sessions/s1")

    assert response.status_code == 200
    data = response.json()
    assert data["succeeded"] is True
    assert set(data["deleted_backends"]) == {"graph", "trace"}
    assert graph.deleted is True
    assert trace.deleted is True
    assert graph.session_present is False
    assert trace.present is False


@pytest.mark.asyncio
async def test_trace_store_delete_session_data_clears_memory_and_disk(tmp_path):
    apply_session_consent({"X-Session-Consent": "opt-in"})
    store = TraceStore(trace_dir=str(tmp_path))
    await store.record(TurnTrace(session_id="s1", turn_number=1))
    await store.record_bg_pass(BackgroundPassTrace(session_id="s1"))
    await store.set_session_metadata("s1", "proxy_config", {"a": 1})

    before = await store.session_storage_summary("s1")
    assert before["present"] is True
    assert before["jsonl_file_exists"] is True

    detail = await store.delete_session_data("s1")
    after = await store.session_storage_summary("s1")

    assert detail["turns_deleted"] == 1
    assert detail["background_passes_deleted"] == 1
    assert detail["metadata_deleted"] is True
    assert detail["jsonl_file_deleted"] is True
    assert after["present"] is False


@pytest.mark.asyncio
async def test_session_consent_required_skips_trace_store_writes(monkeypatch):
    monkeypatch.setenv("SESSION_CONSENT_REQUIRED", "true")
    reset_settings()
    store = TraceStore()

    apply_session_consent({})
    await store.record(TurnTrace(session_id="s1", turn_number=1))
    assert await store.get_session_turns("s1") == []

    apply_session_consent({"X-Session-Consent": "opt-in"})
    await store.record(TurnTrace(session_id="s1", turn_number=2))
    turns = await store.get_session_turns("s1")
    assert len(turns) == 1
    assert turns[0].turn_number == 2


def test_session_consent_required_is_not_session_overridable():
    assert "session_consent_required" in SESSION_CONFIG_DENYLIST
