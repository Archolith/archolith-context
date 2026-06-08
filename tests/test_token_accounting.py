"""Tests for token accounting — models, estimator, client hints, and gate logic."""


from archolith_proxy.token_accounting.models import (
    TokenEstimateBreakdown, TokenTelemetry, GateSource,
)
from archolith_proxy.token_accounting.estimate import (
    estimate_content_tokens, estimate_structural_tokens,
    compute_breakdown, compute_savings, evaluate_gate,
    ESTIMATOR_VERSION,
)
from archolith_proxy.token_accounting.client_hints import (
    extract_client_hint, parse_client_hint_header, parse_client_hint_meta,
)
from archolith_proxy.token_accounting.gating import build_telemetry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMPLE_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant. " * 50},
    {"role": "user", "content": "Hello, how are you? " * 20},
    {"role": "assistant", "content": "I'm doing well, thanks for asking! " * 30},
]

TOOL_CALL_MESSAGES = [
    {"role": "system", "content": "You are a coding assistant. " * 50},
    {"role": "user", "content": "Read the file src/main.py " * 20},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_abc123", "type": "function", "function": {
            "name": "Read", "arguments": "{\"file_path\": \"src/main.py\"}"
        }}
    ]},
    {"role": "tool", "content": "File contents: 331 lines of Python code..." * 30, "tool_call_id": "call_abc123", "name": "Read"},
]

LARGE_TOOLS_ARRAY = [
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read a file from the filesystem",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the file"},
                    "offset": {"type": "integer", "description": "Line offset"},
                    "limit": {"type": "integer", "description": "Max lines"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Glob",
            "description": "Find files matching a glob pattern",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                },
                "required": ["pattern"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestTokenModels:
    def test_breakdown_defaults(self):
        b = TokenEstimateBreakdown()
        assert b.input_tokens_content_est == 0
        assert b.input_tokens_structural_est == 0
        assert b.input_tokens_client_reported is None
        assert b.estimator_version == "v2-structural"
        assert b.savings_tokens_est == 0

    def test_effective_input_structural_only(self):
        b = TokenEstimateBreakdown(input_tokens_structural_est=50000)
        assert b.effective_input == 50000

    def test_effective_input_with_client_higher(self):
        b = TokenEstimateBreakdown(
            input_tokens_structural_est=40000,
            input_tokens_client_reported=60000,
        )
        assert b.effective_input == 60000

    def test_effective_input_with_client_lower(self):
        b = TokenEstimateBreakdown(
            input_tokens_structural_est=50000,
            input_tokens_client_reported=30000,
        )
        assert b.effective_input == 50000  # max wins

    def test_telemetry_to_log_dict(self):
        t = TokenTelemetry(
            breakdown=TokenEstimateBreakdown(
                session_id="test-session",
                turn_number=5,
                input_tokens_content_est=10000,
                input_tokens_structural_est=12000,
            ),
            assembly_mode="graph",
        )
        d = t.to_log_dict()
        assert d["session_id"] == "test-session"
        assert d["turn"] == 5
        assert d["input_content"] == 10000
        assert d["input_structural"] == 12000
        assert d["assembly_mode"] == "graph"

    def test_gate_source_enum(self):
        assert GateSource.STRUCTURAL_ESTIMATE.value == "structural_estimate"
        assert GateSource.MAX_STRUCTURAL_CLIENT.value == "max_structural_client"


# ---------------------------------------------------------------------------
# Estimator tests
# ---------------------------------------------------------------------------

class TestEstimator:
    def test_content_tokens_positive(self):
        tokens = estimate_content_tokens(SIMPLE_MESSAGES)
        assert tokens >= 500  # floor

    def test_structural_tokens_exceeds_content(self):
        content = estimate_content_tokens(TOOL_CALL_MESSAGES)
        structural = estimate_structural_tokens(TOOL_CALL_MESSAGES)
        # Structural includes framing overhead so should be >= content
        # (for messages large enough to exceed the 500 floor)
        assert structural >= content

    def test_structural_tokens_with_tools(self):
        without = estimate_structural_tokens(SIMPLE_MESSAGES)
        with_tools = estimate_structural_tokens(SIMPLE_MESSAGES, tools=LARGE_TOOLS_ARRAY)
        assert with_tools > without  # tool schemas add tokens

    def test_tool_call_messages_counted(self):
        structural = estimate_structural_tokens(TOOL_CALL_MESSAGES)
        content = estimate_content_tokens(TOOL_CALL_MESSAGES)
        # Tool calls add function name + arguments + ID framing
        assert structural > content

    def test_compute_breakdown_basic(self):
        b = compute_breakdown(SIMPLE_MESSAGES, session_id="s1", turn_number=1)
        assert b.input_tokens_content_est >= 500
        assert b.input_tokens_structural_est >= b.input_tokens_content_est
        assert b.gate_input_tokens == b.input_tokens_structural_est  # no client hint
        assert b.gate_source == GateSource.STRUCTURAL_ESTIMATE
        assert b.session_id == "s1"
        assert b.turn_number == 1
        assert b.estimator_version == ESTIMATOR_VERSION

    def test_compute_breakdown_with_client_hint(self):
        b = compute_breakdown(
            SIMPLE_MESSAGES,
            client_reported_tokens=80000,
            session_id="s1", turn_number=1,
        )
        assert b.input_tokens_client_reported == 80000
        assert b.gate_input_tokens == 80000  # max(structural, client) = client
        assert b.gate_source == GateSource.MAX_STRUCTURAL_CLIENT

    def test_compute_breakdown_client_lower_than_structural(self):
        b = compute_breakdown(
            SIMPLE_MESSAGES,
            client_reported_tokens=100,  # way below floor
        )
        # Client is 100 but structural is >= 500
        assert b.gate_input_tokens == b.input_tokens_structural_est

    def test_compute_savings(self):
        b = TokenEstimateBreakdown(
            gate_input_tokens=100000,
            input_tokens_structural_est=100000,
        )
        # Simulate rewritten messages that are smaller
        rewritten = [{"role": "system", "content": "Short context"}]
        result = compute_savings(b, rewritten, graph_context_tokens=5000)
        assert result.rewritten_tokens_est > 0
        assert result.savings_tokens_est > 0
        assert result.savings_ratio_est > 0
        assert result.graph_context_tokens_est == 5000

    def test_compute_savings_zero_when_larger(self):
        b = TokenEstimateBreakdown(
            gate_input_tokens=500,
            input_tokens_structural_est=500,
        )
        # Rewritten messages are larger than original (edge case)
        rewritten = [{"role": "system", "content": "A" * 10000}]
        result = compute_savings(b, rewritten)
        assert result.savings_tokens_est == 0  # max(0, ...)


# ---------------------------------------------------------------------------
# Gate logic tests
# ---------------------------------------------------------------------------

class TestGateLogic:
    def test_cold_start(self):
        b = TokenEstimateBreakdown(gate_input_tokens=10000, gate_source=GateSource.STRUCTURAL_ESTIMATE)
        gate = evaluate_gate(b, turn_number=1, cold_start_turns=3, cold_start_token_threshold=20000)
        assert gate.result == "cold_start"
        assert "turn 1" in gate.reason

    def test_skipped_low_tokens(self):
        b = TokenEstimateBreakdown(gate_input_tokens=30000, gate_source=GateSource.STRUCTURAL_ESTIMATE)
        gate = evaluate_gate(b, turn_number=5, min_input_tokens=55000)
        assert gate.result == "skipped_low_tokens"
        assert "30000" in gate.reason

    def test_skipped_low_savings(self):
        b = TokenEstimateBreakdown(
            gate_input_tokens=100000,
            gate_source=GateSource.STRUCTURAL_ESTIMATE,
            savings_ratio_est=0.05, # 5% savings
        )
        gate = evaluate_gate(b, turn_number=5, min_input_tokens=55000, min_savings_ratio=0.25)
        assert gate.result == "skipped_low_savings"
        assert "5.0%" in gate.reason

    def test_graph_rewrite_approved(self):
        b = TokenEstimateBreakdown(
            gate_input_tokens=100000,
            gate_source=GateSource.STRUCTURAL_ESTIMATE,
            savings_ratio_est=0.45, # 45% savings
        )
        gate = evaluate_gate(b, turn_number=10, min_input_tokens=55000, min_savings_ratio=0.25)
        assert gate.result == "graph"

    def test_gate_with_client_hint(self):
        b = TokenEstimateBreakdown(
            gate_input_tokens=80000,
            gate_source=GateSource.MAX_STRUCTURAL_CLIENT,
        )
        gate = evaluate_gate(b, turn_number=5, min_input_tokens=55000)
        assert gate.result in ("skipped_low_savings", "graph")  # depends on savings
        assert gate.gate_source == GateSource.MAX_STRUCTURAL_CLIENT


# ---------------------------------------------------------------------------
# Client hints tests
# ---------------------------------------------------------------------------

class TestClientHints:
    def test_header_valid(self):
        headers = {"X-Context-Token-Hint": "75000"}
        result = parse_client_hint_header(headers)
        assert result == 75000

    def test_header_case_insensitive(self):
        headers = {"x-context-token-hint": "50000"}
        result = parse_client_hint_header(headers)
        assert result == 50000

    def test_header_invalid(self):
        headers = {"X-Context-Token-Hint": "not_a_number"}
        result = parse_client_hint_header(headers)
        assert result is None

    def test_header_too_low(self):
        headers = {"X-Context-Token-Hint": "5"}
        result = parse_client_hint_header(headers)
        assert result is None  # below MIN_REASONABLE_TOKENS

    def test_header_too_high(self):
        headers = {"X-Context-Token-Hint": "99999999"}
        result = parse_client_hint_header(headers)
        assert result is None  # above MAX_REASONABLE_TOKENS

    def test_header_absent(self):
        headers = {"Content-Type": "application/json"}
        result = parse_client_hint_header(headers)
        assert result is None

    def test_meta_valid(self):
        body = {"_meta": {"context_token_hint": 60000}, "model": "test"}
        result = parse_client_hint_meta(body)
        assert result == 60000

    def test_meta_top_level(self):
        body = {"context_token_hint": 40000, "model": "test"}
        result = parse_client_hint_meta(body)
        assert result == 40000

    def test_meta_invalid(self):
        body = {"_meta": {"context_token_hint": "bad"}, "model": "test"}
        result = parse_client_hint_meta(body)
        assert result is None

    def test_extract_priority_header(self):
        headers = {"X-Context-Token-Hint": "70000"}
        body = {"_meta": {"context_token_hint": 50000}}
        result = extract_client_hint(headers, body)
        assert result == 70000  # header wins

    def test_extract_no_hints(self):
        result = extract_client_hint({}, {})
        assert result is None


# ---------------------------------------------------------------------------
# Integration: build_telemetry
# ---------------------------------------------------------------------------

class TestBuildTelemetry:
    def test_basic_telemetry(self):
        t = build_telemetry(SIMPLE_MESSAGES, session_id="s1", turn_number=1)
        assert t.breakdown.input_tokens_content_est >= 500
        assert t.breakdown.input_tokens_structural_est >= 500
        assert t.assembly_mode in ("cold_start", "passthrough", "graph")
        assert t.gate_decision.result in ("cold_start", "skipped_low_tokens", "graph", "passthrough")

    def test_telemetry_with_tools(self):
        t = build_telemetry(
            TOOL_CALL_MESSAGES,
            tools=LARGE_TOOLS_ARRAY,
            session_id="s1",
            turn_number=1,
        )
        # Structural should count tool schemas
        assert t.breakdown.input_tokens_structural_est >= t.breakdown.input_tokens_content_est

    def test_telemetry_with_client_hint(self):
        t = build_telemetry(
            SIMPLE_MESSAGES,
            client_reported_tokens=90000,
            session_id="s1",
            turn_number=1,
        )
        assert t.breakdown.input_tokens_client_reported == 90000
        assert t.breakdown.gate_source == GateSource.MAX_STRUCTURAL_CLIENT
