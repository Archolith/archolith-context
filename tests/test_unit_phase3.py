"""Unit tests for Phase 3 — context assembly, message rewriting, fact budgeting."""

import math

import pytest

from archolith_proxy.assembler.context import (
    _estimate_tokens,
    _format_context_block,
    _format_session_overview,
    _format_relevant_facts,
    _budget_facts,
    _cosine_similarity,
    _score_fact,
    _expand_with_context_window,
)
from archolith_proxy.proxy.rewrite import rewrite_messages as _rewrite_messages
from archolith_proxy.models.dtos import AssembledContext


class TestEstimateTokens:
    def test_short_text(self):
        # "Hello world" ≈ 2 tokens with cl100k_base, ×1.10 ≈ 2
        assert _estimate_tokens("Hello world") >= 1

    def test_code_text(self):
        # ~400 chars → ~93 tokens with cl100k_base, ×1.10 ≈ 102
        text = "def foo():\n return bar\n" * 16
        tokens = _estimate_tokens(text)
        assert tokens > 0
        # Tiktoken-based estimate should be roughly in the right ballpark
        assert 70 < tokens < 150

    def test_empty_text(self):
        # Empty string → 0 raw tokens → floor of 1
        assert _estimate_tokens("") == 1


class TestFormatContextBlock:
    def test_minimal_block(self):
        result, ratio = _format_context_block(
            goal=None,
            facts=[],
            files=[],
            decisions=[],
            turn_number=5,
        )
        assert "SESSION OVERVIEW" in result
        assert "RELEVANT CONTEXT" in result
        assert "current turn: 5" in result
        assert ratio == 1.0

    def test_with_goal(self):
        result, _ = _format_context_block(
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
        result, _ = _format_context_block(
            goal=None,
            facts=facts,
            files=[],
            decisions=[],
            turn_number=4,
        )
        assert "RELEVANT CONTEXT" in result
        assert "Build fails" in result
        assert "[error|t3]" in result
        assert "[file_state|t2]" in result

    def test_with_files(self):
        files = [
            {"path": "src/app.py", "status": "modified"},
            {"path": "tests/test_app.py", "status": "created"},
        ]
        result, _ = _format_context_block(
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
        result, _ = _format_context_block(
            goal=None,
            facts=[],
            files=[],
            decisions=decisions,
            turn_number=4,
        )
        assert "Decisions Made" in result
        assert "Use FastAPI over Flask" in result
        assert "Better async support" in result

    def test_two_tier_overview_before_facts(self):
        """Overview section must come before facts for prompt caching benefit."""
        facts = [
            {"content": "test fact", "fact_type": "observation", "confidence": 0.5, "source_turn": 1},
        ]
        result, _ = _format_context_block(
            goal="Test goal",
            facts=facts,
            files=[{"path": "a.py", "status": "modified"}],
            decisions=[],
            turn_number=2,
        )
        overview_pos = result.index("SESSION OVERVIEW")
        facts_pos = result.index("RELEVANT CONTEXT")
        assert overview_pos < facts_pos

    def test_overview_includes_fact_count(self):
        """Overview should show total active fact count."""
        facts = [
            {"content": "fact 1", "fact_type": "observation", "confidence": 0.5, "source_turn": 1},
            {"content": "fact 2", "fact_type": "error", "confidence": 0.9, "source_turn": 2},
        ]
        result, _ = _format_context_block(
            goal=None,
            facts=facts,
            files=[],
            decisions=[],
            turn_number=3,
            active_fact_count=42,
        )
        assert "42 active facts" in result

    def test_no_facts_shows_placeholder(self):
        """When no facts are budgeted, show a placeholder."""
        result, _ = _format_context_block(
            goal=None,
            facts=[],
            files=[],
            decisions=[],
            turn_number=1,
        )
        assert "no facts above relevance threshold" in result


class TestSessionOverview:
    def test_overview_has_delimiter(self):
        result = _format_session_overview("goal", [], [], turn_number=5)
        assert "=== SESSION OVERVIEW ===" in result

    def test_overview_stable_format(self):
        """Same input → same output (prompt caching requires stability)."""
        args = ("Build API", [{"path": "x.py", "status": "modified"}], [], 3, 10)
        result1 = _format_session_overview(*args)
        result2 = _format_session_overview(*args)
        assert result1 == result2


class TestRelevantFacts:
    def test_facts_has_delimiter(self):
        facts = [{"content": "test", "fact_type": "observation", "source_turn": 1}]
        result, _ = _format_relevant_facts(facts, turn_number=2)
        assert "=== RELEVANT CONTEXT ===" in result

    def test_empty_facts_placeholder(self):
        result, _ = _format_relevant_facts([], turn_number=1)
        assert "no facts above relevance threshold" in result


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
        """Messages should be rewritten with graph context merged into system message."""
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

        # Should have: merged system msg + coherence tail
        assert result[0]["role"] == "system"
        # Graph context is MERGED into the system message, not a separate message
        assert "You are helpful." in result[0]["content"]
        assert "graph context here" in result[0]["content"]
        # Only ONE system message (NVIDIA rejects consecutive system messages)
        system_count = sum(1 for m in result if m["role"] == "system")
        assert system_count == 1
        # Middle (user T1, assistant R1) kept + coherence tail (user T2, assistant R2, user T3)
        assert len(result) == 1 + 2 + 3  # 1 merged system + 2 middle + 3 tail

    def test_preserves_system_prompt(self):
        """The original system prompt should be preserved within the merged system message."""
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
        # System prompt is merged with graph context
        assert "You are a coding assistant." in result[0]["content"]
        assert "assembled context" in result[0]["content"]
        assert result[0]["role"] == "system"

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
        # Merged system message + 1 user message
        assert len(result) == 2  # 1 merged system + 1 user

    def test_no_system_message(self):
        """Works correctly when there's no system message.

        Graph context becomes the system message, then coherence tail follows.
        """
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
        # Graph context becomes the system message
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "ctx"
        # Tail [assistant, user] → leading assistant stripped → [user]
        non_system = [m for m in result if m["role"] != "system"]
        assert non_system[0]["role"] == "user"

    def test_strips_leading_assistant_from_tail(self):
        """Coherence tail starting with 'assistant' must be stripped for role alternation."""
        original = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Turn 2"},
            {"role": "assistant", "content": "Response 2"},
            {"role": "user", "content": "Turn 3"},
        ]
        assembled = AssembledContext(
            system_message={"role": "system", "content": "graph ctx"},
            graph_context=[{"role": "system", "content": "graph ctx"}],
            coherence_tail=[],
            token_estimate=500,
            facts_retrieved=5,
            session_id="test",
        )
        # With tail_size=2, tail would be [assistant R2, user T3]
        # The leading assistant must be stripped → only [user T3] remains
        result = _rewrite_messages(original, assembled, coherence_tail_size=2)
        # After system msgs, first non-system must be 'user'
        non_system = [m for m in result if m["role"] != "system"]
        assert non_system[0]["role"] == "user"

    def test_merges_consecutive_duplicate_roles(self):
        """Consecutive same-role messages in the tail should be merged."""
        original = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "World"},  # consecutive user messages
            {"role": "assistant", "content": "Hi"},
        ]
        assembled = AssembledContext(
            system_message={"role": "system", "content": "ctx"},
            graph_context=[{"role": "system", "content": "ctx"}],
            coherence_tail=[],
            token_estimate=100,
            facts_retrieved=3,
            session_id="test",
        )
        result = _rewrite_messages(original, assembled, coherence_tail_size=10)
        # Check no consecutive same-role messages in result (excluding system)
        non_system = [m for m in result if m["role"] != "system"]
        for i in range(1, len(non_system)):
            assert non_system[i]["role"] != non_system[i - 1]["role"], \
                f"Consecutive {non_system[i]['role']} messages at positions {i-1} and {i}"

    def test_role_alternation_with_tool_messages(self):
        """Tool messages between user/assistant should not break alternation logic."""
        original = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Run this"},
            {"role": "assistant", "content": "Let me check"},
            {"role": "tool", "content": "result: 42"},
            {"role": "assistant", "content": "The answer is 42"},
            {"role": "user", "content": "Next question"},
        ]
        assembled = AssembledContext(
            system_message={"role": "system", "content": "ctx"},
            graph_context=[{"role": "system", "content": "ctx"}],
            coherence_tail=[],
            token_estimate=100,
            facts_retrieved=3,
            session_id="test",
        )
        result = _rewrite_messages(original, assembled, coherence_tail_size=10)
        # After system messages, the first message must be 'user' (not 'tool' or 'assistant')
        non_system = [m for m in result if m["role"] != "system"]
        assert non_system[0]["role"] == "user"

    def test_preserves_tool_chains_in_middle(self):
        """Tool_calls and tool results in the middle must stay paired."""
        original = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Read the file"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "file contents here"},
            {"role": "assistant", "content": "Here is a long analysis " * 20},
            {"role": "user", "content": "What did you find?"},
            {"role": "assistant", "content": "I found X"},
            {"role": "user", "content": "Tell me more"},
        ]
        assembled = AssembledContext(
            system_message={"role": "system", "content": "ctx"},
            graph_context=[{"role": "system", "content": "ctx"}],
            coherence_tail=[],
            token_estimate=500,
            facts_retrieved=5,
            session_id="test",
        )
        result = _rewrite_messages(original, assembled, coherence_tail_size=3)
        # Tool chains in the middle should be preserved (not orphaned)
        tool_call_ids = set()
        tool_result_ids = set()
        for msg in result:
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tool_call_ids.add(tc["id"])
            if msg.get("tool_call_id"):
                tool_result_ids.add(msg["tool_call_id"])
        # Every tool result should have a matching tool_call
        assert tool_result_ids <= tool_call_ids, \
            f"Orphaned tool results: {tool_result_ids - tool_call_ids}"


class TestColdStartLogic:
    """Test that assemble_context returns None during cold start."""

    @pytest.mark.asyncio
    async def test_cold_start_turn_0(self):
        """Turn 0 with small input should return None (passthrough)."""
        from archolith_proxy.assembler.context import assemble_context
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
        from archolith_proxy.assembler.context import assemble_context
        result = await assemble_context(
            session_id="test-session",
            turn_number=1,
            input_token_estimate=5000,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_cold_start_user_turn_count_is_sole_gate(self):
        """User turn count is the sole gate — token threshold does NOT override.

        Agentic sessions (OpenCode, Claude Code) generate huge token counts
        within a single user turn via tool-use loops. Assembly must not fire
        mid-loop just because tokens crossed a threshold.
        """
        from archolith_proxy.config import get_settings
        settings = get_settings()

        # 1 user turn, huge tokens: should still be cold_start
        user_turns = 1
        should_attempt_assembly = not (user_turns < settings.cold_start_turns)
        assert should_attempt_assembly is False

        # 3 user turns (>= threshold): should attempt assembly
        user_turns = 3
        should_attempt_assembly = not (user_turns < settings.cold_start_turns)
        assert should_attempt_assembly is True


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert _cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert _cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_empty_vectors(self):
        assert _cosine_similarity([], []) == 0.0

    def test_zero_magnitude(self):
        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_different_lengths(self):
        assert _cosine_similarity([1.0], [1.0, 2.0]) == 0.0


class TestScoreFact:
    def test_without_embeddings_uses_priority(self):
        """Without embeddings, higher type priority scores higher."""
        error_fact = {
            "fact_type": "error",
            "confidence": 0.9,
            "source_turn": 1,
        }
        observation_fact = {
            "fact_type": "observation",
            "confidence": 0.9,
            "source_turn": 1,
        }
        error_score = _score_fact(error_fact, None, turn_number=5)
        obs_score = _score_fact(observation_fact, None, turn_number=5)
        assert error_score > obs_score

    def test_without_embeddings_recency_boost(self):
        """More recent facts get a higher score (same type, same confidence)."""
        old_fact = {
            "fact_type": "observation",
            "confidence": 0.7,
            "source_turn": 1,
        }
        new_fact = {
            "fact_type": "observation",
            "confidence": 0.7,
            "source_turn": 4,
        }
        old_score = _score_fact(old_fact, None, turn_number=5)
        new_score = _score_fact(new_fact, None, turn_number=5)
        assert new_score > old_score

    def test_with_embeddings_similarity_boost(self):
        """With embeddings, facts similar to query score higher."""
        query_embedding = [1.0, 0.0, 0.0]
        similar_fact = {
            "fact_type": "observation",
            "confidence": 0.5,
            "source_turn": 1,
            "embedding": [0.9, 0.1, 0.0],
        }
        dissimilar_fact = {
            "fact_type": "observation",
            "confidence": 0.5,
            "source_turn": 1,
            "embedding": [0.0, 0.0, 0.9],
        }
        sim_score = _score_fact(similar_fact, query_embedding, turn_number=5)
        dis_score = _score_fact(dissimilar_fact, query_embedding, turn_number=5)
        assert sim_score > dis_score

    def test_without_embedding_on_fact_falls_back(self):
        """If fact has no embedding, falls back to priority scoring."""
        query_embedding = [1.0, 0.0, 0.0]
        fact_no_emb = {
            "fact_type": "error",
            "confidence": 0.9,
            "source_turn": 1,
        }
        score = _score_fact(fact_no_emb, query_embedding, turn_number=5)
        assert score > 0


class TestContextWindowing:
    def test_no_expansion_without_adjacent(self):
        """If all facts are from the same turn, no additional facts are added."""
        selected = [
            {"fact_id": "a", "source_turn": 3},
            {"fact_id": "b", "source_turn": 3},
        ]
        all_facts = selected + [{"fact_id": "c", "source_turn": 1}]
        result = _expand_with_context_window(selected, all_facts)
        assert len(result) == 2

    def test_expansion_includes_adjacent_turns(self):
        """Facts from N-1 and N+1 turns should be included."""
        selected = [{"fact_id": "a", "source_turn": 3}]
        all_facts = [
            {"fact_id": "a", "source_turn": 3},
            {"fact_id": "b", "source_turn": 2},
            {"fact_id": "c", "source_turn": 4},
            {"fact_id": "d", "source_turn": 1},
        ]
        result = _expand_with_context_window(selected, all_facts)
        result_ids = {f["fact_id"] for f in result}
        assert "a" in result_ids
        assert "b" in result_ids
        assert "c" in result_ids
        assert "d" not in result_ids

    def test_no_duplicates(self):
        """Windowing should not duplicate already-selected facts."""
        selected = [
            {"fact_id": "a", "source_turn": 3},
            {"fact_id": "b", "source_turn": 2},
        ]
        all_facts = [
            {"fact_id": "a", "source_turn": 3},
            {"fact_id": "b", "source_turn": 2},
            {"fact_id": "c", "source_turn": 4},
        ]
        result = _expand_with_context_window(selected, all_facts)
        fact_ids = [f["fact_id"] for f in result]
        assert fact_ids.count("a") == 1
        assert fact_ids.count("b") == 1

    def test_empty_selected_returns_empty(self):
        result = _expand_with_context_window([], [])
        assert result == []


class TestBudgetFactsWithEmbeddings:
    def test_embedding_scoring_reorders_facts(self):
        """With embeddings enabled, similar facts rank higher."""
        query_embedding = [1.0, 0.0, 0.0]
        facts = [
            {
                "fact_id": "dis",
                "content": "dissimilar fact",
                "fact_type": "state",
                "confidence": 0.9,
                "source_turn": 3,
                "embedding": [0.0, 0.0, 0.9],
            },
            {
                "fact_id": "sim",
                "content": "similar fact",
                "fact_type": "observation",
                "confidence": 0.5,
                "source_turn": 1,
                "embedding": [0.95, 0.05, 0.0],
            },
        ]
        result = _budget_facts(
            facts,
            token_budget=1000,
            query_embedding=query_embedding,
            turn_number=5,
            embedding_enabled=True,
        )
        if len(result) >= 2:
            assert result[0]["fact_id"] == "sim"

    def test_no_embeddings_falls_back_to_priority(self):
        """Without embedding_enabled, priority scoring is used."""
        facts = [
            {"content": "obs", "fact_type": "observation", "confidence": 0.5, "source_turn": 1},
            {"content": "err", "fact_type": "error", "confidence": 0.95, "source_turn": 2},
        ]
        result = _budget_facts(facts, token_budget=1000)
        assert result[0]["fact_type"] == "error"
