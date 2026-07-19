"""Unit tests for trace builder, store, and DTOs."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from archolith_proxy.models.dtos import TurnTrace, SessionTraceSummary, AssembledContext, TRACE_VERSION
from archolith_proxy.trace.builder import TraceBuilder
from archolith_proxy.trace.store import TraceStore, reset_trace_store


class TestTurnTraceDTO:
    """Test TurnTrace model defaults and field types."""

    def test_defaults(self):
        trace = TurnTrace()
        assert trace.session_id is None
        assert trace.turn_number == 0
        assert trace.trace_version == TRACE_VERSION
        assert trace.assembly_mode == "passthrough"
        assert trace.input_tokens == 0
        assert trace.facts_selected == []
        assert trace.original_messages == []
        assert trace.rewritten_messages == []
        assert trace.fallback_reason == ""
        assert trace.recall_used is False

    def test_turn_id_auto_generated(self):
        trace = TurnTrace()
        assert len(trace.turn_id) == 16  # uuid4 hex[:16]

    def test_turn_id_unique(self):
        t1 = TurnTrace()
        t2 = TurnTrace()
        assert t1.turn_id != t2.turn_id

    def test_created_at_populated(self):
        before = time.time()
        trace = TurnTrace()
        after = time.time()
        assert before <= trace.created_at <= after

    def test_full_construction(self):
        trace = TurnTrace(
            session_id="sess-123",
            turn_number=5,
            model="gpt-4",
            stream=True,
            input_tokens=1000,
            assembly_mode="graph",
            assembly_reason="sufficient facts",
            assembly_latency_ms=42.5,
            rewritten_tokens=600,
            savings_tokens=400,
            savings_ratio=0.4,
            facts_selected=[{"content": "fact1"}],
            original_messages=[{"role": "user", "content": "hello"}],
            rewritten_messages=[{"role": "user", "content": "hello"}],
            upstream_status=200,
            upstream_latency_ms=1500.0,
            facts_stored=3,
            duplicates_skipped=1,
            invalidations_attempted=0,
            recall_used=True,
            recall_question="what files were modified?",
            recall_facts_returned=2,
        )
        assert trace.session_id == "sess-123"
        assert trace.turn_number == 5
        assert trace.savings_ratio == 0.4
        assert trace.recall_used is True

    def test_model_dump_roundtrip(self):
        trace = TurnTrace(session_id="s1", turn_number=1, input_tokens=500)
        data = trace.model_dump()
        restored = TurnTrace(**data)
        assert restored.session_id == trace.session_id
        assert restored.turn_number == trace.turn_number
        assert restored.turn_id == trace.turn_id


class TestSessionTraceSummaryDTO:
    def test_defaults(self):
        summary = SessionTraceSummary(session_id="s1")
        assert summary.turn_count == 0
        assert summary.total_input_tokens == 0
        assert summary.avg_savings_ratio == 0.0
        assert summary.assembly_modes == {}
        assert summary.total_recalls == 0

    def test_full_construction(self):
        summary = SessionTraceSummary(
            session_id="s1",
            goal="test goal",
            turn_count=10,
            total_input_tokens=50000,
            total_savings_tokens=20000,
            avg_savings_ratio=0.4,
            assembly_modes={"graph": 7, "passthrough": 3},
            total_facts_stored=25,
            total_duplicates_skipped=5,
            total_invalidations_attempted=2,
            total_recalls=4,
        )
        assert summary.goal == "test goal"
        assert summary.assembly_modes["graph"] == 7


class TestAssembledContextDTO:
    """Test AssembledContext has files_selected and decisions_selected fields."""

    def test_defaults_empty_lists(self):
        ctx = AssembledContext(system_message={"role": "system", "content": "test"}, graph_context=[], coherence_tail=[])
        assert ctx.files_selected == []
        assert ctx.decisions_selected == []

    def test_files_selected_populated(self):
        ctx = AssembledContext(
            system_message={"role": "system", "content": "test"},
            graph_context=[],
            coherence_tail=[],
            files_selected=[{"path": "/foo.py", "status": "modified"}],
            decisions_selected=[{"summary": "use X", "turn": 3}],
        )
        assert len(ctx.files_selected) == 1
        assert ctx.files_selected[0]["path"] == "/foo.py"
        assert len(ctx.decisions_selected) == 1
        assert ctx.decisions_selected[0]["summary"] == "use X"

    def test_model_dump_roundtrip(self):
        ctx = AssembledContext(
            system_message={"role": "system", "content": "test"},
            graph_context=[],
            coherence_tail=[],
            files_selected=[{"path": "/a.py"}],
            decisions_selected=[{"summary": "dec"}],
        )
        data = ctx.model_dump()
        restored = AssembledContext(**data)
        assert restored.files_selected == ctx.files_selected
        assert restored.decisions_selected == ctx.decisions_selected


class TestTraceBuilder:
    """Test the incremental TraceBuilder."""

    def test_empty_build(self):
        builder = TraceBuilder()
        trace = builder.build()
        assert trace.session_id is None
        assert trace.turn_number == 0
        assert trace.input_tokens == 0

    def test_set_request(self):
        builder = TraceBuilder()
        builder.set_request(
            session_id="sess-abc",
            turn_number=3,
            model="claude-3",
            stream=True,
            input_tokens=2000,
            message_count=10,
        )
        trace = builder.build()
        assert trace.session_id == "sess-abc"
        assert trace.turn_number == 3
        assert trace.model == "claude-3"
        assert trace.stream is True
        assert trace.input_tokens == 2000
        assert trace.message_count == 10

    def test_set_token_telemetry_surfaces_in_trace(self):
        """Structural token telemetry + actual upstream prompt_tokens persist
        through build() (guards against the DTO silently dropping extra keys)."""
        from archolith_proxy.token_accounting import build_telemetry

        builder = TraceBuilder()
        # Tiny message (content estimate hits its floor) but several realistic tool
        # schemas, so structural must exceed content once the tools are counted.
        tools = [
            {"type": "function", "function": {
                "name": f"search_documents_{i}",
                "description": (
                    "Search the indexed corpus for documents matching a natural "
                    "language query and return ranked results with snippets and scores."
                ),
                "parameters": {"type": "object", "properties": {
                    f"query_{j}": {"type": "string", "description": f"The {j}th search expression to evaluate against the corpus."}
                    for j in range(6)
                }},
            }}
            for i in range(8)
        ]
        tel = build_telemetry([{"role": "user", "content": "fix it"}], tools=tools)
        builder.set_token_telemetry(tel.breakdown)
        builder.set_response(status=200, prompt_tokens=1234)
        trace = builder.build()

        # Structural counts the tool schema the content-only estimate misses.
        assert trace.token_structural_est > trace.token_content_est
        assert trace.token_gate_input == tel.breakdown.gate_input_tokens
        assert trace.token_gate_source  # non-empty source label
        assert trace.token_estimator_version
        # Actual upstream input tokens captured for estimate-vs-actual.
        assert trace.prompt_tokens_actual == 1234

    def test_set_assembly(self):
        builder = TraceBuilder()
        builder.set_assembly(
            mode="graph",
            reason="sufficient context",
            latency_ms=55.0,
            facts_selected=[{"content": "f1"}],
            files_selected=[{"path": "/foo.py"}],
            decisions_selected=[{"summary": "use X"}],
            rewritten_tokens=500,
            savings_tokens=1500,
            savings_ratio=0.75,
        )
        trace = builder.build()
        assert trace.assembly_mode == "graph"
        assert trace.assembly_reason == "sufficient context"
        assert trace.assembly_latency_ms == 55.0
        assert len(trace.facts_selected) == 1
        assert len(trace.files_selected) == 1
        assert trace.files_selected[0]["path"] == "/foo.py"
        assert len(trace.decisions_selected) == 1
        assert trace.decisions_selected[0]["summary"] == "use X"
        assert trace.savings_tokens == 1500

    def test_set_assembly_defaults(self):
        """files_selected and decisions_selected default to [] when not passed."""
        builder = TraceBuilder()
        builder.set_assembly(mode="passthrough")
        trace = builder.build()
        assert trace.files_selected == []
        assert trace.decisions_selected == []

    def test_set_response(self):
        builder = TraceBuilder()
        builder.set_response(
            status=200,
            latency_ms=1200.0,
            output_tokens=150,
            response_summary="Here is the answer",
        )
        trace = builder.build()
        assert trace.upstream_status == 200
        assert trace.upstream_latency_ms == 1200.0
        assert trace.output_tokens == 150
        assert trace.upstream_response_summary == "Here is the answer"

    def test_set_extraction(self):
        builder = TraceBuilder()
        builder.set_extraction(
            facts_stored=4,
            duplicates_skipped=2,
            invalidations_attempted=1,
            invalidations_matched=1,
            extraction_latency_ms=350.0,
            extracted_facts=[{"content": "x", "type": "observation"}],
        )
        trace = builder.build()
        assert trace.facts_stored == 4
        assert trace.duplicates_skipped == 2
        assert trace.invalidations_attempted == 1
        assert trace.invalidations_matched == 1
        assert trace.extraction_latency_ms == 350.0

    def test_set_recall(self):
        builder = TraceBuilder()
        builder.set_recall(used=True, question="what files?", facts_returned=3)
        trace = builder.build()
        assert trace.recall_used is True
        assert trace.recall_question == "what files?"
        assert trace.recall_facts_returned == 3
        assert trace.recall_trigger == "model_invoked"

    def test_set_recall_preserves_explicit_trigger(self):
        builder = TraceBuilder()
        builder.set_recall(
            used=True,
            question="what files?",
            facts_returned=3,
            trigger="proxy_forced:user_phrase",
        )
        trace = builder.build()
        assert trace.recall_trigger == "proxy_forced:user_phrase"

    def test_set_filter_and_outbound_context_stats(self):
        builder = TraceBuilder()
        builder.set_filter_stats(
            available=True,
            chars_saved=120,
            chars_before=1000,
            chars_after=880,
        )
        builder.set_outbound_context_stats(outbound_chars_sent=930, proxy_recall_chars_added=50)
        trace = builder.build()
        assert trace.filter_available is True
        assert trace.filter_chars_saved == 120
        assert trace.filter_chars_before == 1000
        assert trace.filter_chars_after == 880
        assert trace.outbound_chars_sent == 930
        assert trace.proxy_recall_chars_added == 50
        assert trace.filter_strategy_savings == {"request_filter": 120}

    def test_set_solo_stats_adds_strategy_breakdown(self):
        builder = TraceBuilder()
        builder.set_solo_stats({
            "strategies_applied": ["compact", "dedup"],
            "chars_saved_compact": 400,
            "chars_saved_dedup": 250,
            "chars_saved_curator_cache": 100,
            "total_chars_saved": 750,
        })
        trace = builder.build()
        assert trace.solo_strategies == ["compact", "dedup"]
        assert trace.solo_chars_saved_compact == 400
        assert trace.solo_chars_saved_dedup == 250
        assert trace.solo_chars_saved_curator == 100
        assert trace.filter_strategy_savings == {
            "curator_cache": 100,
            "compact": 400,
            "dedup": 250,
        }

    def test_set_fallback_reason(self):
        builder = TraceBuilder()
        builder.set_fallback_reason("Neo4j connection failed")
        trace = builder.build()
        assert trace.fallback_reason == "Neo4j connection failed"

    def test_incremental_build(self):
        """Builder can be built multiple times as data accumulates."""
        builder = TraceBuilder()
        builder.set_request("s1", 1, "gpt-4", False, 1000, 5)
        trace1 = builder.build()
        assert trace1.input_tokens == 1000
        assert trace1.facts_stored == 0  # Not set yet

        builder.set_extraction(facts_stored=3)
        trace2 = builder.build()
        assert trace2.facts_stored == 3
        assert trace2.input_tokens == 1000  # Previous data preserved

    def test_set_original_messages_truncation(self):
        """Messages with content longer than 2000 chars get truncated."""
        builder = TraceBuilder()
        long_msg = [{"role": "user", "content": "x" * 3000}]
        builder.set_original_messages(long_msg)
        trace = builder.build()
        stored = trace.original_messages[0]["content"]
        assert "truncated" in stored
        assert len(stored) < 3000

    def test_set_response_summary_truncation(self):
        """Response summaries longer than 500 chars get truncated."""
        builder = TraceBuilder()
        builder.set_response(status=200, response_summary="y" * 600)
        trace = builder.build()
        assert len(trace.upstream_response_summary) <= 500

    def test_recall_question_truncation(self):
        """Recall questions longer than 200 chars get truncated."""
        builder = TraceBuilder()
        builder.set_recall(used=True, question="q" * 300)
        trace = builder.build()
        assert len(trace.recall_question) <= 200


class TestTraceStore:
    """Test the in-memory TraceStore."""

    def setup_method(self):
        """Reset the singleton before each test."""
        reset_trace_store()

    @pytest.mark.asyncio
    async def test_record_and_get_turn(self):
        store = TraceStore()
        trace = TurnTrace(session_id="s1", turn_number=1, input_tokens=500)
        await store.record(trace)
        retrieved = await store.get_turn(trace.turn_id)
        assert retrieved is not None
        assert retrieved.session_id == "s1"
        assert retrieved.turn_id == trace.turn_id

    @pytest.mark.asyncio
    async def test_get_turn_not_found(self):
        store = TraceStore()
        result = await store.get_turn("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_session_turns_pagination(self):
        store = TraceStore()
        for i in range(5):
            await store.record(TurnTrace(session_id="s1", turn_number=i))
        turns = await store.get_session_turns("s1", limit=3, offset=0)
        assert len(turns) == 3
        turns2 = await store.get_session_turns("s1", limit=3, offset=3)
        assert len(turns2) == 2

    @pytest.mark.asyncio
    async def test_session_summary(self):
        store = TraceStore()
        await store.record(TurnTrace(session_id="s1", turn_number=1, input_tokens=1000, savings_tokens=300, assembly_mode="graph", recall_used=True))
        await store.record(TurnTrace(session_id="s1", turn_number=2, input_tokens=2000, savings_tokens=600, assembly_mode="passthrough", recall_used=False))
        summary = await store.get_session_summary("s1")
        assert summary is not None
        assert summary.turn_count == 2
        assert summary.total_input_tokens == 3000
        assert summary.total_savings_tokens == 900
        assert summary.avg_savings_ratio == 0.3
        assert summary.assembly_modes == {"graph": 1, "passthrough": 1}
        assert summary.total_recalls == 1

    @pytest.mark.asyncio
    async def test_session_summary_not_found(self):
        store = TraceStore()
        result = await store.get_session_summary("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        store = TraceStore()
        await store.record(TurnTrace(session_id="s1", turn_number=1))
        await store.record(TurnTrace(session_id="s2", turn_number=1))
        summaries = await store.list_sessions()
        assert len(summaries) == 2
        ids = {s.session_id for s in summaries}
        assert ids == {"s1", "s2"}

    @pytest.mark.asyncio
    async def test_eviction_on_max_turns(self):
        store = TraceStore(max_turns_per_session=3)
        traces = []
        for i in range(5):
            t = TurnTrace(session_id="s1", turn_number=i)
            await store.record(t)
            traces.append(t)
        # Only last 3 should be kept
        turns = await store.get_session_turns("s1")
        assert len(turns) == 3
        # First 2 should be evicted
        for t in traces[:2]:
            result = await store.get_turn(t.turn_id)
            assert result is None

    @pytest.mark.asyncio
    async def test_total_traces_counter(self):
        store = TraceStore()
        for i in range(3):
            await store.record(TurnTrace(session_id="s1", turn_number=i))
        assert store.total_traces == 3

    @pytest.mark.asyncio
    async def test_session_count(self):
        store = TraceStore()
        await store.record(TurnTrace(session_id="s1", turn_number=1))
        await store.record(TurnTrace(session_id="s2", turn_number=1))
        assert store.session_count == 2

    @pytest.mark.asyncio
    async def test_no_session_uses_placeholder(self):
        """Traces with no session_id get stored under __no_session__."""
        store = TraceStore()
        trace = TurnTrace(session_id=None, turn_number=0, input_tokens=100)
        await store.record(trace)
        # Should be retrievable by turn_id
        retrieved = await store.get_turn(trace.turn_id)
        assert retrieved is not None
        assert retrieved.input_tokens == 100


class TestTraceStoreDiskPersistence:
    """Test optional disk persistence for trace records."""

    @pytest.mark.asyncio
    async def test_disk_persistence_creates_jsonl(self, tmp_path):
        """When trace_dir is set, traces are written as JSONL files."""
        store = TraceStore(trace_dir=str(tmp_path))
        trace = TurnTrace(session_id="s1", turn_number=1, input_tokens=50)
        await store.record(trace)

        jsonl_path = tmp_path / "s1.jsonl"
        assert jsonl_path.exists()
        content = jsonl_path.read_text(encoding="utf-8").strip()
        assert len(content) > 0
        # Should be valid JSON
        import json
        data = json.loads(content)
        assert data["session_id"] == "s1"
        assert data["input_tokens"] == 50

    @pytest.mark.asyncio
    async def test_disk_persistence_appends(self, tmp_path):
        """Multiple traces to the same session append to the same file."""
        store = TraceStore(trace_dir=str(tmp_path))
        for i in range(3):
            t = TurnTrace(session_id="s1", turn_number=i, input_tokens=i * 10)
            await store.record(t)

        jsonl_path = tmp_path / "s1.jsonl"
        lines = jsonl_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

    @pytest.mark.asyncio
    async def test_disk_persistence_no_dir_means_no_files(self, tmp_path):
        """When trace_dir is None, no files are written."""
        store = TraceStore()
        await store.record(TurnTrace(session_id="s1", turn_number=1))
        # tmp_path should be empty (no jsonl files)
        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) == 0

    @pytest.mark.asyncio
    async def test_disk_persistence_session_id_sanitized(self, tmp_path):
        """Session IDs with slashes are sanitized for filesystem safety."""
        store = TraceStore(trace_dir=str(tmp_path))
        trace = TurnTrace(session_id="path/with/slashes", turn_number=1)
        await store.record(trace)

        jsonl_path = tmp_path / "path_with_slashes.jsonl"
        assert jsonl_path.exists()

    @pytest.mark.asyncio
    async def test_verify_consistency_reports_orphans_and_mismatches(self):
        store = TraceStore()
        await store.record(TurnTrace(session_id="orphan", turn_number=2))
        await store.record(TurnTrace(session_id="mismatch", turn_number=5))

        async def _graph_turn(session_id: str) -> int:
            return {"orphan": 0, "mismatch": 2}.get(session_id, 1)

        backend = AsyncMock()
        backend.get_turn_number = AsyncMock(side_effect=_graph_turn)

        report = await store.verify_consistency(backend=backend)

        assert any("orphan" in item for item in report["orphans"])
        assert any("mismatch" in item for item in report["mismatches"])


class TestTraceBuilderExtraction:
    """Test that TraceBuilder.set_extraction captures all extraction fields."""

    def test_set_extraction_full(self):
        builder = TraceBuilder()
        builder.set_request("s1", 1, "model", False, 100, 1)
        builder.set_extraction(
            facts_stored=3,
            duplicates_skipped=2,
            invalidations_attempted=1,
            invalidations_matched=1,
            extraction_latency_ms=150.0,
            extracted_facts=[{"content": "fact1", "type": "observation"}],
        )
        trace = builder.build()
        assert trace.facts_stored == 3
        assert trace.duplicates_skipped == 2
        assert trace.invalidations_attempted == 1
        assert trace.invalidations_matched == 1
        assert trace.extraction_latency_ms == 150.0
        assert len(trace.extracted_facts) == 1
        assert trace.extracted_facts[0]["content"] == "fact1"

    def test_set_extraction_defaults(self):
        builder = TraceBuilder()
        builder.set_request("s1", 1, "model", False, 100, 1)
        builder.set_extraction()
        trace = builder.build()
        assert trace.facts_stored == 0
        assert trace.duplicates_skipped == 0
        assert trace.invalidations_attempted == 0
        assert trace.invalidations_matched == 0
        assert trace.extraction_latency_ms == 0.0
        assert trace.extracted_facts == []
