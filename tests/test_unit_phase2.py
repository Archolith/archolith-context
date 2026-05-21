"""Phase 2 unit tests — fingerprinting, extraction parsing, label-guard."""

import pytest

from archolith_proxy.proxy.session import compute_fingerprint, sanitize_system_prompt
from archolith_proxy.extractor.client import _parse_extraction_response
from archolith_proxy.graph.repository import _validate_cypher, LabelGuardViolation, CONTEXT_SESSION_LABEL
from archolith_proxy.models.dtos import ExtractionResult


# --- Fingerprinting ---


def test_fingerprint_deterministic():
    """Same inputs → same fingerprint."""
    fp1 = compute_fingerprint("system prompt", "hello world")
    fp2 = compute_fingerprint("system prompt", "hello world")
    assert fp1 == fp2
    assert len(fp1) == 16


def test_fingerprint_different_inputs():
    """Different inputs → different fingerprints."""
    fp1 = compute_fingerprint("system prompt", "hello")
    fp2 = compute_fingerprint("system prompt", "goodbye")
    assert fp1 != fp2


def test_fingerprint_different_system_prompts():
    """Different system prompts → different fingerprints."""
    fp1 = compute_fingerprint("system A", "hello")
    fp2 = compute_fingerprint("system B", "hello")
    assert fp1 != fp2


def test_sanitize_strips_timestamps():
    """Dynamic timestamps are stripped before fingerprinting."""
    prompt = "You are an assistant.\nCurrent date: 2026-05-09\nDo good work."
    cleaned = sanitize_system_prompt(prompt)
    assert "2026-05-09" not in cleaned
    assert "You are an assistant." in cleaned
    assert "Do good work." in cleaned


def test_sanitize_strips_iso_timestamps():
    """ISO-8601 timestamps are stripped."""
    prompt = "System prompt\nTimestamp: 2026-05-09T14:30:00Z\nMore instructions."
    cleaned = sanitize_system_prompt(prompt)
    assert "2026-05-09T14:30:00Z" not in cleaned
    assert "System prompt" in cleaned


def test_sanitized_fingerprint_stable():
    """Fingerprint is stable despite dynamic timestamp changes."""
    p1 = "You are an assistant.\nCurrent date: 2026-05-09\nDo good work."
    p2 = "You are an assistant.\nCurrent date: 2026-05-10\nDo good work."
    fp1 = compute_fingerprint(p1, "hello")
    fp2 = compute_fingerprint(p2, "hello")
    assert fp1 == fp2


def test_sanitize_strips_tool_definition_blocks():
    """Tool definition blocks in system prompts are stripped before fingerprinting."""
    prompt = (
        "You are an assistant.\n"
        "Available tools: [{\"name\": \"read_file\", \"description\": \"Read a file\"}]\n"
        "Do good work."
    )
    cleaned = sanitize_system_prompt(prompt)
    assert "read_file" not in cleaned
    assert "You are an assistant." in cleaned
    assert "Do good work." in cleaned


def test_sanitize_strips_tool_definitions_heading():
    """Tool definitions with 'Tool definitions:' prefix are stripped."""
    prompt = (
        "System prompt\n"
        "Tool definitions: [{\"name\": \"bash\", \"description\": \"Run command\"}]\n"
        "More instructions."
    )
    cleaned = sanitize_system_prompt(prompt)
    assert "bash" not in cleaned
    assert "System prompt" in cleaned
    assert "More instructions." in cleaned


def test_sanitize_strips_json_tool_schema_lines():
    """Individual JSON tool schema lines (name/description/parameters) are stripped."""
    prompt = (
        "You are an assistant.\n"
        '  "name": "edit_file",\n'
        '  "description": "Edit a file",\n'
        '  "parameters": {"type": "object"},\n'
        "Do good work."
    )
    cleaned = sanitize_system_prompt(prompt)
    assert "edit_file" not in cleaned
    assert "You are an assistant." in cleaned
    assert "Do good work." in cleaned


def test_fingerprint_stable_across_tool_changes():
    """Fingerprint is stable when tool definitions change between turns."""
    p1 = "You are an assistant.\nAvailable tools: [{\"name\": \"read_file\"}]\nDo good work."
    p2 = "You are an assistant.\nAvailable tools: [{\"name\": \"read_file\"}, {\"name\": \"write_file\"}]\nDo good work."
    fp1 = compute_fingerprint(p1, "hello")
    fp2 = compute_fingerprint(p2, "hello")
    assert fp1 == fp2


# --- Extraction parsing ---


def test_parse_valid_json():
    """Valid extraction JSON is parsed correctly."""
    content = '''{"facts": [{"content": "src/main.py was modified", "fact_type": "file_state", "confidence": 0.9}], "files_touched": [{"path": "src/main.py", "status": "modified"}], "decisions": [], "invalidated": []}'''
    result = _parse_extraction_response(content, turn_number=3)
    assert isinstance(result, ExtractionResult)
    assert len(result.facts) == 1
    assert result.facts[0]["content"] == "src/main.py was modified"
    assert result.files_touched == ["src/main.py"]
    assert result.turn_number == 3


def test_parse_markdown_fenced_json():
    """JSON wrapped in markdown code fences is extracted."""
    content = '```json\n{"facts": [], "files_touched": [], "decisions": [], "invalidated": []}\n```'
    result = _parse_extraction_response(content, turn_number=1)
    assert isinstance(result, ExtractionResult)
    assert result.facts == []


def test_parse_invalid_json_returns_empty():
    """Invalid JSON returns empty result, doesn't crash."""
    content = "This is not JSON at all"
    result = _parse_extraction_response(content, turn_number=1)
    assert result.facts == []
    assert result.turn_number == 1


def test_parse_missing_fields():
    """JSON with missing fields uses defaults."""
    content = '{"facts": [{"content": "test", "fact_type": "observation"}]}'
    result = _parse_extraction_response(content, turn_number=2)
    assert len(result.facts) == 1
    assert result.files_touched == []
    assert result.decisions == []


# --- Label-guard repository ---


def test_label_guard_accepts_labeled_query():
    """Queries with :ContextSession label pass validation."""
    cypher = f"MATCH (n:{CONTEXT_SESSION_LABEL} {{session_id: $id}}) RETURN n"
    _validate_cypher(cypher)  # Should not raise


def test_label_guard_rejects_unlabeled_query():
    """Queries without :ContextSession label raise LabelGuardViolation."""
    cypher = "MATCH (n {session_id: $id}) RETURN n"
    with pytest.raises(LabelGuardViolation):
        _validate_cypher(cypher)


def test_label_guard_accepts_create_with_label():
    """CREATE with :ContextSession label passes validation."""
    cypher = f"CREATE (n:{CONTEXT_SESSION_LABEL}:Session {{session_id: $id}}) RETURN n"
    _validate_cypher(cypher)  # Should not raise


def test_label_guard_accepts_merge_with_label():
    """MERGE with :ContextSession label passes validation."""
    cypher = f"MERGE (n:{CONTEXT_SESSION_LABEL}:File {{path: $path}}) RETURN n"
    _validate_cypher(cypher)  # Should not raise


def test_label_guard_rejects_generic_match():
    """Generic MATCH without label is rejected."""
    cypher = "MATCH (n) RETURN n"
    with pytest.raises(LabelGuardViolation):
        _validate_cypher(cypher)
