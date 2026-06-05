"""Tests for scripts/redundancy.py — classification of file-read redundancy."""

from __future__ import annotations

import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import pytest

import redundancy  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — build small inline message-list fixtures
# ---------------------------------------------------------------------------

_COUNTER = 0


def _next_id() -> str:
    global _COUNTER
    _COUNTER += 1
    return f"call_{_COUNTER}"


def _make_tool_call(
    tool_name: str,
    file_path: str,
    call_id: str | None = None,
) -> dict:
    """Create an assistant tool_calls entry (the *call*, not the result)."""
    cid = call_id if call_id else _next_id()
    return {
        "id": cid,
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": json.dumps({"file_path": file_path}),
        },
    }


def _make_tool_result(
    content: str,
    call_id: str,
    tool_name: str | None = None,
) -> dict:
    """Create a tool-result message (role='tool')."""
    msg: dict = {"role": "tool", "tool_call_id": call_id, "content": content}
    if tool_name is not None:
        msg["name"] = tool_name
    return msg


def _make_assistant_with_calls(
    calls: list[dict],
    content: str = "",
) -> dict:
    """Create an assistant message that issued tool calls."""
    return {"role": "assistant", "content": content, "tool_calls": calls}


def _make_user_msg(content: str = "") -> dict:
    return {"role": "user", "content": content}


LONG_CONTENT: str = "abc " * 20  # >= 80 chars, >= 20 tokens (80//4 = 20)
OTHER_CONTENT: str = "xyz " * 20  # different content, same length


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExactDuplicate:
    """Two identical reads → the second is a dup."""

    def test_exact_duplicate_read_counted_once_as_dup(self) -> None:
        cid1 = _next_id()
        cid2 = _next_id()

        messages = [
            _make_assistant_with_calls([_make_tool_call("read_file", "foo.py", cid1)]),
            _make_tool_result(LONG_CONTENT, cid1, "read_file"),
            _make_assistant_with_calls([_make_tool_call("read_file", "foo.py", cid2)]),
            _make_tool_result(LONG_CONTENT, cid2, "read_file"),
        ]
        report = redundancy.classify_read_redundancy(messages)

        assert report.total_reads == 2
        # First read is NOT a dup, second IS
        assert report.exact_dup_reads == 1
        assert report.exact_dup_tokens > 0
        assert report.live_reads == 1
        assert report.live_tokens > 0


class TestFullWriteSupersedes:
    """A completed full-file write after a read → read is superseded."""

    def test_full_write_supersedes_prior_read(self) -> None:
        read_id = _next_id()
        write_id = _next_id()

        messages = [
            _make_assistant_with_calls([_make_tool_call("read_file", "a.py", read_id)]),
            _make_tool_result(LONG_CONTENT, read_id, "read_file"),
            _make_assistant_with_calls([_make_tool_call("write_file", "a.py", write_id)]),
            _make_tool_result("written ok", write_id, "write_file"),
        ]
        report = redundancy.classify_read_redundancy(messages)

        assert report.total_reads == 1
        assert report.superseded_reads == 1
        assert report.superseded_tokens > 0
        assert report.live_reads == 0
        assert report.exact_dup_reads == 0


class TestPartialEditNoSupersede:
    """A partial edit (edit_file) does NOT supersede a prior read."""

    def test_partial_edit_does_not_supersede(self) -> None:
        read_id = _next_id()
        edit_id = _next_id()

        messages = [
            _make_assistant_with_calls([_make_tool_call("read_file", "a.py", read_id)]),
            _make_tool_result(LONG_CONTENT, read_id, "read_file"),
            _make_assistant_with_calls([_make_tool_call("edit_file", "a.py", edit_id)]),
            _make_tool_result("edited", edit_id, "edit_file"),
        ]
        report = redundancy.classify_read_redundancy(messages)

        assert report.total_reads == 1
        assert report.live_reads == 1
        assert report.live_tokens == redundancy.estimate_tokens(LONG_CONTENT)
        assert report.superseded_reads == 0


class TestFilePathKey:
    """Real harnesses (OpenCode/Claude) use camelCase 'filePath' for the path."""

    def test_filepath_camelcase_supersession(self) -> None:
        read_id = _next_id()
        write_id = _next_id()
        read_call = {
            "id": read_id, "type": "function",
            "function": {"name": "read", "arguments": json.dumps({"filePath": "a.py"})},
        }
        write_call = {
            "id": write_id, "type": "function",
            "function": {"name": "write", "arguments": json.dumps({"filePath": "a.py"})},
        }
        messages = [
            _make_assistant_with_calls([read_call]),
            _make_tool_result(LONG_CONTENT, read_id, "read"),
            _make_assistant_with_calls([write_call]),
            _make_tool_result("ok", write_id, "write"),
        ]
        report = redundancy.classify_read_redundancy(messages)
        assert report.superseded_reads == 1  # path resolved via filePath
        assert report.live_reads == 0


class TestLiveRead:
    """A single read with no later write and no duplicate → live."""

    def test_live_read_untouched(self) -> None:
        cid = _next_id()

        messages = [
            _make_assistant_with_calls([_make_tool_call("cat", "main.py", cid)]),
            _make_tool_result(LONG_CONTENT, cid, "cat"),
        ]
        report = redundancy.classify_read_redundancy(messages)

        assert report.total_reads == 1
        assert report.live_reads == 1
        assert report.live_tokens == redundancy.estimate_tokens(LONG_CONTENT)
        assert report.exact_dup_reads == 0
        assert report.superseded_reads == 0


class TestPrecedence:
    """Dup takes priority over superseded when both conditions are true."""

    def test_precedence_dup_over_superseded(self) -> None:
        # Read #1: distinct path with NO later write → live (first occurrence).
        # Read #2: same content as #1 (so it is a dup) AND its path gets a later
        #          full-write (so it would also qualify as superseded). Dup must
        #          win — proving the precedence ordering.
        cid1 = _next_id()
        cid2 = _next_id()
        write_id = _next_id()

        messages = [
            _make_assistant_with_calls([_make_tool_call("read", "other.py", cid1)]),
            _make_tool_result(LONG_CONTENT, cid1, "read"),
            # Read #2: same content, different path that gets written later
            _make_assistant_with_calls([_make_tool_call("read", "shared.py", cid2)]),
            _make_tool_result(LONG_CONTENT, cid2, "read"),
            # Later full-write to read #2's path
            _make_assistant_with_calls(
                [_make_tool_call("write_file", "shared.py", write_id)]
            ),
            _make_tool_result("done", write_id, "write_file"),
        ]
        report = redundancy.classify_read_redundancy(messages)

        assert report.total_reads == 2
        # Read #1 = live (distinct path, no later write)
        assert report.live_reads == 1
        # Read #2 = dup (same content as read #1) — dup precedence over superseded
        assert report.exact_dup_reads == 1
        assert report.superseded_reads == 0


class TestRatios:
    """Ratio fields in to_dict() behave correctly."""

    def test_ratios_sum_and_zero_safe(self) -> None:
        # --- Session with reads ---
        cid1 = _next_id()
        cid2 = _next_id()
        cid3 = _next_id()
        w_id = _next_id()

        messages = [
            # Read #1 (a.txt, later written → superseded)
            _make_assistant_with_calls([_make_tool_call("read_file", "a.txt", cid1)]),
            _make_tool_result(LONG_CONTENT, cid1, "read_file"),
            # Read #2 (dup of read #1)
            _make_assistant_with_calls([_make_tool_call("read_file", "b.txt", cid2)]),
            _make_tool_result(LONG_CONTENT, cid2, "read_file"),
            # Read #3 (different content, no dup, but later write → superseded)
            _make_assistant_with_calls([_make_tool_call("cat", "a.txt", cid3)]),
            _make_tool_result(OTHER_CONTENT, cid3, "cat"),
            # Full write to a.txt after the reads
            _make_assistant_with_calls(
                [_make_tool_call("create_file", "a.txt", w_id)]
            ),
            _make_tool_result("written", w_id, "create_file"),
        ]
        report = redundancy.classify_read_redundancy(messages)
        d = report.to_dict()

        assert d["total_reads"] == 3
        assert d["total_read_tokens"] > 0
        # Ratios should sum to ~1.0
        ratio_sum = d["exact_dup_ratio"] + d["superseded_ratio"] + d["live_ratio"]
        assert ratio_sum == pytest.approx(1.0, rel=1e-4)

        # --- Empty session ---
        empty_report = redundancy.RedundancyReport()
        d2 = empty_report.to_dict()
        assert d2["exact_dup_ratio"] == 0.0
        assert d2["superseded_ratio"] == 0.0
        assert d2["live_ratio"] == 0.0
        # No ZeroDivisionError
        assert d2["total_read_tokens"] == 0
