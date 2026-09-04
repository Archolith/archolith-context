"""Tests for proxy-forced recall trigger detection and its per-session debounce.

Regression context: `repeated_file_read` was gated behind `is_user_turn`, which
made it unreachable in agent loops -- the only turns that can exhibit a repeated
file read are continuation turns, and those are never user turns. A 91-turn
baseline replay fired proxy recall 0 times as a result.
"""

from __future__ import annotations

import pytest

from archolith_proxy.proxy.recall import (
    RecallTrigger,
    detect_recall_trigger,
    mark_files_recalled,
    new_repeated_files,
    reset_recall_ledger,
)


@pytest.fixture(autouse=True)
def _clear_ledger():
    reset_recall_ledger()
    yield
    reset_recall_ledger()


def _read(path: str, name: str = "Read") -> dict:
    return {"role": "tool", "name": name, "content": f"{path}\n1  some code"}


def _repeated_read_history(path: str = "/repo/a.py", last_role: str = "assistant") -> list[dict]:
    """History where `path` was read twice, ending on a non-user turn."""
    return [
        {"role": "user", "content": "work through the plan"},
        {"role": "assistant", "content": "reading"},
        _read(path),
        {"role": "assistant", "content": "reading again"},
        _read(path),
        {"role": last_role, "content": "continuing"},
    ]


# ── Trigger 2: the gate regression ────────────────────────────────────────────


def test_repeated_read_fires_on_agent_solo_turn():
    """The original bug: this returned None for every continuation turn."""
    trigger = detect_recall_trigger(_repeated_read_history(), is_user_turn=False)

    assert trigger is not None
    assert trigger.trigger_type == "repeated_file_read"
    assert trigger.files == ("/repo/a.py",)


def test_repeated_read_still_fires_on_user_turn():
    history = _repeated_read_history(last_role="user")
    trigger = detect_recall_trigger(history, is_user_turn=True)

    assert trigger is not None
    assert trigger.trigger_type == "repeated_file_read"


def test_single_read_does_not_fire():
    history = [
        {"role": "assistant", "content": "reading"},
        _read("/repo/a.py"),
        {"role": "assistant", "content": "done"},
    ]
    assert detect_recall_trigger(history, is_user_turn=False) is None


def test_two_different_files_do_not_fire():
    history = [
        _read("/repo/a.py"),
        _read("/repo/b.py"),
        {"role": "assistant", "content": "done"},
    ]
    assert detect_recall_trigger(history, is_user_turn=False) is None


def test_repeated_file_ordering_is_deterministic_on_ties():
    """Ledger keys depend on file order; equal hit counts must not reorder."""
    history = [
        _read("/repo/b.py"),
        _read("/repo/a.py"),
        _read("/repo/b.py"),
        _read("/repo/a.py"),
        {"role": "assistant", "content": "done"},
    ]
    first = detect_recall_trigger(history, is_user_turn=False)
    second = detect_recall_trigger(list(reversed(history[:-1])) + [history[-1]], is_user_turn=False)

    assert first is not None and second is not None
    assert first.files == ("/repo/a.py", "/repo/b.py")
    assert first.files == second.files


def test_non_read_tools_are_ignored():
    history = [
        {"role": "tool", "name": "Bash", "content": "/repo/a.py\noutput"},
        {"role": "tool", "name": "Bash", "content": "/repo/a.py\noutput"},
        {"role": "assistant", "content": "done"},
    ]
    assert detect_recall_trigger(history, is_user_turn=False) is None


# ── Trigger 1 stays user-turn-only ────────────────────────────────────────────


def test_user_phrase_fires_on_user_turn():
    trigger = detect_recall_trigger(
        [{"role": "user", "content": "Remind me what we decided"}], is_user_turn=True
    )
    assert trigger is not None
    assert trigger.trigger_type == "user_phrase"
    assert trigger.files == ()


def test_user_phrase_does_not_fire_on_agent_solo_turn():
    """A stale user message further back must not re-trigger on continuations."""
    history = [
        {"role": "user", "content": "remind me what we decided"},
        {"role": "assistant", "content": "sure"},
        {"role": "assistant", "content": "still working"},
    ]
    assert detect_recall_trigger(history, is_user_turn=False) is None


def test_user_phrase_reads_multipart_content():
    trigger = detect_recall_trigger(
        [{"role": "user", "content": [{"type": "text", "text": "what did we decide about X"}]}],
        is_user_turn=True,
    )
    assert trigger is not None
    assert trigger.trigger_type == "user_phrase"


def test_baseline_first_turn_does_not_fire():
    """Regression guard for the trace that prompted this: no phrase, no history."""
    history = [
        {"role": "user", "content": "look at the two-curator plan and work through the plan"}
    ]
    assert detect_recall_trigger(history, is_user_turn=True) is None


def test_empty_messages_do_not_fire():
    assert detect_recall_trigger([], is_user_turn=True) is None
    assert detect_recall_trigger([], is_user_turn=False) is None


# ── Debounce ledger ───────────────────────────────────────────────────────────


def test_ledger_suppresses_second_firing_for_same_file():
    trigger = detect_recall_trigger(_repeated_read_history(), is_user_turn=False)
    assert trigger is not None

    assert new_repeated_files("s1", trigger.files) == ["/repo/a.py"]
    mark_files_recalled("s1", trigger.files)

    # Next turn: window still matches, but the file is already recalled.
    assert new_repeated_files("s1", trigger.files) == []


def test_ledger_allows_a_newly_repeated_file():
    mark_files_recalled("s1", ("/repo/a.py",))

    assert new_repeated_files("s1", ("/repo/a.py", "/repo/b.py")) == ["/repo/b.py"]


def test_ledger_is_scoped_per_session():
    mark_files_recalled("s1", ("/repo/a.py",))

    assert new_repeated_files("s2", ("/repo/a.py",)) == ["/repo/a.py"]


def test_unmarked_files_stay_eligible():
    """A failed or empty recall must not suppress the next turn's attempt."""
    assert new_repeated_files("s1", ("/repo/a.py",)) == ["/repo/a.py"]
    assert new_repeated_files("s1", ("/repo/a.py",)) == ["/repo/a.py"]


def test_empty_session_id_disables_debounce():
    mark_files_recalled("", ("/repo/a.py",))
    assert new_repeated_files("", ("/repo/a.py",)) == ["/repo/a.py"]


def test_per_session_file_set_is_bounded():
    from archolith_proxy.proxy.recall import _MAX_FILES_PER_SESSION, _recalled_files

    mark_files_recalled("s1", tuple(f"/repo/f{i}.py" for i in range(_MAX_FILES_PER_SESSION + 50)))

    assert len(_recalled_files["s1"]) == _MAX_FILES_PER_SESSION
    # Oldest evicted, newest retained.
    assert new_repeated_files("s1", ("/repo/f0.py",)) == ["/repo/f0.py"]
    assert new_repeated_files("s1", (f"/repo/f{_MAX_FILES_PER_SESSION + 49}.py",)) == []


def test_session_ledger_is_bounded():
    from archolith_proxy.proxy.recall import _MAX_LEDGER_SESSIONS, _recalled_files

    for i in range(_MAX_LEDGER_SESSIONS + 10):
        mark_files_recalled(f"s{i}", ("/repo/a.py",))

    assert len(_recalled_files) == _MAX_LEDGER_SESSIONS


def test_recall_trigger_is_immutable():
    trigger = RecallTrigger("repeated_file_read", "q", ("/repo/a.py",))
    with pytest.raises(Exception):
        trigger.trigger_type = "user_phrase"  # type: ignore[misc]
