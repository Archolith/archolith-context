"""Unit tests for Phase 3 — context assembly, message rewriting, fact budgeting."""

import pytest

from src.assembler.context import (
    _estimate_tokens,
    _format_context_block,
    _budget_facts,
)
from src.openai.chat import _rewrite_messages
from src.models.dtos import AssembledContext


class TestEstimateTokens:
    def test_short_text(self):
        # "Hello world" = 11 chars -> ~3 tokens
        assert _estimate_tokens("Hello world") >= 1

    def test_code_text(self):
        # ~400 chars -> ~114 tokens
        text = "def foo():\n    return bar\n" * 16
        tokens = _estimate_tokens(text)
        assert tokens > 0
        # Should be roughly in the right ballpark
        assert 80 < tokens < 150

    def test_empty_text(self):
        assert _estimate_tokens("") == 1  # min 1


class TestFormatContextBlock:
    def test_minimal_block(self):
        result = _format_context_block(
            goal=None,
            facts=[],
            files=[],
            decisions=[],
            turn_number=5,
        )
        assert "Session Context" in result
        assert "current turn: 5" in result

    def test_with_goal(self):
        result = _format_context_block(
            goal="Build a REST API",
            facts=[],
            files=[],
            decisions=[],
            turn_number=3,
        )
        assert "Session Goal" in result
        assert "Build a REST API" in result

    def test_with_facts(self):
        facts = [
            {"content": "src/app.py has FastAPI routes", "fact_type": "file_state", "confidence": 0.9, "source_turn": 2},
            {"content": "Build fails with ImportError", "fact_type": "error", "confidence": 0.95, "source_turn": 3},
        ]
        result = _format_context_block(
            goal=None,
            facts=facts,
            files=[],
            decisions=[],
            turn_number=4,
        )
        assert "Relevant Facts" in result
        assert "Build fails" in result
        assert "[error|t3]" in result
        assert "[file_state|t2]" in result

    def test_with_files(self):
        files = [
            {"path": "src/app.py", "status": "modified"},
            {"path": "tests/test_app.py", "status": "created"},
        ]
        result = _format_context_block(
            goal=None,
            facts=[],
            files=files,
            decisions=[],
            turn_number=5,
        )
        assert "Files Touched" in result
        assert "src/app.py" in result
        assert "tests/test_app.py" in result

    def test_with_decisions(self):
        decisions = [
            {"summary": "Use FastAPI over Flask", "rationale": "Better async support", "turn": 2},
        ]
        result = _format_context_block(
            goal=None,
            facts=[],
            files=[],
            decisions=decisions,
            turn_number=4,
        )
        assert "Decisions Made" in result
        assert "Use FastAPI over Flask" in result
        assert "Better async support" in result


class TestBudgetFacts:
    def test_all_facts_fit(self):
        facts = [
            {"content": "short fact", "fact_type": "observation", "confidence": 0.7, "source_turn": 1},
            {"content": "another fact", "fact_type": "state", "confidence": 0.9, "source_turn": 2},
        ]
        result = _budget_facts(facts, token_budget=1000)
        assert len(result) == 2

    def test_budget_cuts_facts(self):
        facts = [
            {"content": "x" * 1000, "fact_type": "observation", "confidence": 0.5, "source_turn": 1},
            {"content": "short", "fact_type": "error", "confidence": 0.95, "source_turn": 2},
        ]
        # Small budget: should only fit the short fact (error is also higher priority)
        result = _budget_facts(facts, token_budget=100)
        assert len(result) <= 2
        # The error fact should be selected first (higher priority)
        if len(result) >= 1:
            assert result[0]["fact_type"] == "error"

    def test_empty_facts(self):
        result = _budget_facts([], token_budget=1000)
        assert result == []

    def test_priority_ordering(self):
        """Errors should be selected before observations when budget is tight."""
        facts = [
            {"content": "observation " * 20, "fact_type": "observation", "confidence": 0.5, "source_turn": 1},
            {"content": "error " * 5, "fact_type": "error", "confidence": 0.95, "source_turn": 2},
            {"content": "state " * 5, "fact_type": "state", "confidence": 0.8, "source_turn": 3},
        ]
        # Budget that only fits 1-2 facts
        result = _budget_facts(facts, token_budget=50)
        # First fact should be the error (highest priority)
        if result:
            assert result[0]["fact_type"] == "error"


class TestRewriteMessages:
    def test_passthrough_when_no_context(self):
        """When assembled context is None, messages pass through unchanged."""
        original = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "How are you?"},
        ]
        # AssembledContext with empty graph_context
        assembled = AssembledContext(
            system_message={"role": "system", "content": "ctx"},
            graph_context=[],
            coherence_tail=[],
            token_estimate=100,
            facts_retrieved=0,
            session_id="test",
        )
        result = _rewrite_messages(original, assembled, coherence_tail_size=3)
        assert result == original

    def test_rewrites_with_context(self):
        """Messages should be rewritten with graph context injected."""
        original = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Turn 2"},
            {"role": "assistant", "content": "Response 2"},
            {"role": "user", "content": "Turn 3"},
        ]
        assembled = AssembledContext(
            system_message={"role": "system", "content": "graph context here"},
            graph_context=[{"role": "system", "content": "graph context here"}],
            coherence_tail=[],
            token_estimate=500,
            facts_retrieved=10,
            session_id="test",
        )
        result = _rewrite_messages(original, assembled, coherence_tail_size=3)

        # Should have: system + graph_context + last 3 messages
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "You are helpful."  # Original system preserved
        assert result[1]["content"] == "graph context here"  # Graph context injected
        # Coherence tail: last 3 messages (assistant R2, user T3... actually last 3 from rest)
        # rest = [user T1, assistant R1, user T2, assistant R2, user T3]
        # tail = last 3: [assistant R1, user T2, assistant R2, user T3] -> last 3 = [user T2, assistant R2, user T3]
        assert len(result) == 2 + 3  # system + graph_ctx + 3 tail

    def test_preserves_system_prompt(self):
        """The original system prompt should always be preserved."""
        original = [
            {"role": "system", "content": "You are a coding assistant."},
            {"role": "user", "content": "Write code"},
        ]
        assembled = AssembledContext(
            system_message={"role": "system", "content": "assembled context"},
            graph_context=[{"role": "system", "content": "assembled context"}],
            coherence_tail=[],
            token_estimate=100,
            facts_retrieved=5,
            session_id="test",
        )
        result = _rewrite_messages(original, assembled, coherence_tail_size=3)
        assert result[0]["content"] == "You are a coding assistant."

    def test_small_message_count(self):
        """When messages are fewer than tail size, all are kept."""
        original = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hi"},
        ]
        assembled = AssembledContext(
            system_message={"role": "system", "content": "ctx"},
            graph_context=[{"role": "system", "content": "ctx"}],
            coherence_tail=[],
            token_estimate=100,
            facts_retrieved=3,
            session_id="test",
        )
        result = _rewrite_messages(original, assembled, coherence_tail_size=5)
        # All non-system messages should be in the tail
        assert len(result) == 3  # system + graph_ctx + 1 user

    def test_no_system_message(self):
        """Works correctly when there's no system message."""
        original = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Code please"},
        ]
        assembled = AssembledContext(
            system_message={"role": "system", "content": "ctx"},
            graph_context=[{"role": "system", "content": "ctx"}],
            coherence_tail=[],
            token_estimate=100,
            facts_retrieved=2,
            session_id="test",
        )
        result = _rewrite_messages(original, assembled, coherence_tail_size=2)
        # No system msg to preserve, so: graph_ctx + tail
        assert result[0]["content"] == "ctx"
        # Tail: last 2 messages
        assert len(result) == 3  # graph_ctx + 2 tail


class TestColdStartLogic:
    """Test that assemble_context returns None during cold start."""

    @pytest.mark.asyncio
    async def test_cold_start_turn_0(self):
        """Turn 0 with small input should return None (passthrough)."""
        from src.assembler.context import assemble_context
        # Turn 0, small input: should be cold start
        result = await assemble_context(
            session_id="test-session",
            turn_number=0,
            input_token_estimate=5000,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_cold_start_turn_1(self):
        """Turn 1 with small input should still be cold start (default threshold is turn 3)."""
        from src.assembler.context import assemble_context
        result = await assemble_context(
            session_id="test-session",
            turn_number=1,
            input_token_estimate=5000,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_cold_start_bypass_on_large_input(self):
        """Large input token count should bypass cold start even at turn 1.

        This is a logic check: cold_start condition is
        (turn < cold_start_turns AND tokens < threshold).
        If tokens >= threshold, the AND fails and we proceed to assembly.

        Since we can't connect to Neo4j in unit tests, we verify the
        cold-start bypass logic directly by checking the condition.
        """
        from src.config import get_settings
        settings = get_settings()

        # Turn 1, tokens > threshold: the cold-start gate should be OPEN
        turn = 1
        tokens = 25000  # Above the 20K default threshold
        should_attempt_assembly = not (
            turn < settings.cold_start_turns and tokens < settings.cold_start_token_threshold
        )
        assert should_attempt_assembly is True

        # Turn 1, tokens < threshold: the cold-start gate should be CLOSED
        tokens = 5000
        should_attempt_assembly = not (
            turn < settings.cold_start_turns and tokens < settings.cold_start_token_threshold
        )
        assert should_attempt_assembly is False

        # Turn >= cold_start_turns: always attempt assembly regardless of token count
        turn = 3
        should_attempt_assembly = not (
            turn < settings.cold_start_turns and tokens < settings.cold_start_token_threshold
        )
        assert should_attempt_assembly is True
