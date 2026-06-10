"""Regression tests: proxy filter path must never dedup-marker across requests.

The 2026-06-10 doom loop: filter_output Stage 7 used a process-global
DedupeTracker, so re-sent history content got replaced by a marker one
request after first appearance — the upstream model lost file content it
had just read and re-read in slices forever.

The fix: filter_adapter passes a FRESH DedupeTracker per filter_output call
(payload-replay semantics). These tests exercise the REAL archolith_filter
package (no mocks) and skip if it is not installed.
"""

from __future__ import annotations

import importlib

import pytest

from archolith_proxy import filter_adapter

archolith_filter = pytest.importorskip("archolith_filter")


@pytest.fixture(autouse=True)
def _fresh_adapter_and_singleton():
    """Reset adapter sentinels and the archolith_filter global singleton."""
    importlib.reload(filter_adapter)
    archolith_filter.reset_dedupe_tracker()
    yield
    archolith_filter.reset_dedupe_tracker()


def _payload(content: str) -> list[dict]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "read the file"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": content},
    ]


# Long enough to clear every min-chars threshold in the filter pipeline.
_FILE_CONTENT = "\n".join(
    f"line {i}: def function_{i}(): return compute_value({i})" for i in range(80)
)


def _normalize_ids(text: str) -> str:
    """Strip the per-call raw_output_id counter from truncation footers."""
    import re

    return re.sub(r"raw_output_id=\d+", "raw_output_id=N", text)


def test_same_payload_twice_content_survives_both_requests():
    """The doom-loop regression: re-sent history is never markered.

    The category filter may legitimately trim content (head/tail truncation),
    but the second request must produce BYTE-IDENTICAL output to the first —
    cross-request idempotence — and never a dedupe marker that replaces the
    content wholesale.
    """
    first = filter_adapter.filter_tool_messages(_payload(_FILE_CONTENT), enabled=True)
    second = filter_adapter.filter_tool_messages(_payload(_FILE_CONTENT), enabled=True)

    first_content = first[-1]["content"]
    second_content = second[-1]["content"]

    for label, content in (("first", first_content), ("second", second_content)):
        assert "superseded" not in content, label
        assert "[repeated output" not in content, label
        # A dedupe marker replaces everything; real content keeps its head.
        assert content.startswith("line 0:"), label

    # The core invariant: re-sending the same payload yields identical output.
    # raw_output_id in the truncation footer is a per-call store counter and is
    # normalized out (known nondeterminism — tracked separately as a
    # cache-stability issue, not a dedup defect).
    assert _normalize_ids(first_content) == _normalize_ids(second_content)


def test_filter_single_tool_result_not_markered_across_calls():
    """Extraction-path helper: same content on consecutive turns stays intact."""
    out1 = filter_adapter.filter_single_tool_result(_FILE_CONTENT, tool_name="read")
    out2 = filter_adapter.filter_single_tool_result(_FILE_CONTENT, tool_name="read")
    assert "[repeated output" not in out1
    assert "[repeated output" not in out2
    assert _normalize_ids(out1) == _normalize_ids(out2)
    assert out2.startswith("line 0:")


def test_global_singleton_untouched_by_proxy_path():
    """The proxy path must not pollute the live-stream singleton tracker."""
    filter_adapter.filter_tool_messages(_payload(_FILE_CONTENT), enabled=True)
    singleton = archolith_filter.get_dedupe_tracker()
    assert singleton.size == 0
