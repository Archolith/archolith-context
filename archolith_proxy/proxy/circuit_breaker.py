"""Per-session circuit breaker for synthetic tool re-injection.

Prevents runaway error loops when synthetic tools fail persistently:
- After N consecutive failures, disable synthetic injection for a cooldown period.
- After N total failures in a session, hard-disable for the session lifetime.
- State is in-memory only (resets on proxy restart).
- Bounded to max 10,000 sessions with LRU-style eviction.

The circuit breaker is checked in chat.py BEFORE calling inject_synthetic_tools().
Failures are recorded when _handle_non_streaming catches a synthetic interception
error or when _fallback_strip_synthetic is invoked (indicating the re-send failed).

Thread-safety: The accessor functions are sync (called from async context within
the single event loop). A threading.Lock guards mutations to _sessions since
async/await do not yield control during these quick dict operations.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

__all__ = [
    "SessionCircuitState",
    "get_circuit_state",
    "is_synthetic_allowed",
    "record_synthetic_failure",
    "record_synthetic_success",
    "reset_circuit",
    "reset_all",
    "get_all_circuit_states",
    "add_session_tokens",
    "is_session_over_budget",
]

# Maximum number of sessions to track before evicting oldest entries
_MAX_CIRCUIT_SESSIONS = 10_000

# Lock protecting _sessions from concurrent mutation.
# Reentrant: several public accessors hold the lock and then call
# get_circuit_state(), which re-acquires it on the same thread.
_sessions_lock = threading.RLock()


@dataclass
class SessionCircuitState:
    """Circuit breaker state for a single session."""

    consecutive_failures: int = 0
    total_failures: int = 0
    disabled_until: float = 0.0  # monotonic time; 0 = not disabled
    hard_disabled: bool = False  # lifetime disable after max total failures
    total_input_tokens: int = 0  # cumulative input tokens for budget tracking


# ── Session state store ──────────────────────────────────────────────────────

_sessions: OrderedDict[str, SessionCircuitState] = OrderedDict()


def get_circuit_state(session_id: str) -> SessionCircuitState:
    """Get or create circuit state for a session."""
    with _sessions_lock:
        if session_id not in _sessions:
            # Evict oldest if at capacity
            while len(_sessions) >= _MAX_CIRCUIT_SESSIONS:
                oldest_key, _ = _sessions.popitem(last=False)
                logger.debug("circuit_state_evicted", session_id=oldest_key,
                              reason="capacity", max=_MAX_CIRCUIT_SESSIONS)
            _sessions[session_id] = SessionCircuitState()
        elif session_id in _sessions:
            # Move to end to maintain LRU ordering
            _sessions.move_to_end(session_id)
        return _sessions[session_id]


def is_synthetic_allowed(session_id: str) -> bool:
    """Check whether synthetic tool injection is allowed for this session.

    Returns True if the circuit is closed (injection OK), False if open.
    Also recovers the circuit if the cooldown window has elapsed.
    """
    with _sessions_lock:
        state = get_circuit_state(session_id)

        if state.hard_disabled:
            return False

        if state.disabled_until > 0:
            now = time.monotonic()
            if now >= state.disabled_until:
                # Cooldown elapsed — reset consecutive counter, allow injection
                state.consecutive_failures = 0
                state.disabled_until = 0.0
                logger.info(
                    "synthetic_circuit_recovered",
                    session_id=session_id,
                    total_failures=state.total_failures,
                )
                return True
            return False

        return True


def record_synthetic_failure(
    session_id: str,
    max_consecutive: int = 3,
    cooldown_seconds: float = 300.0,
    max_total: int = 10,
) -> None:
    """Record a synthetic tool failure and update circuit state.

    Args:
        session_id: Session identifier.
        max_consecutive: Consecutive failures before opening the circuit.
        cooldown_seconds: Seconds to keep the circuit open.
        max_total: Total failures before hard-disabling for session lifetime.
    """
    from archolith_proxy.metrics import record_metric
    record_metric("synthetic_tool_failures")

    with _sessions_lock:
        state = get_circuit_state(session_id)
        state.consecutive_failures += 1
        state.total_failures += 1

        if state.total_failures >= max_total:
            state.hard_disabled = True
            record_metric("synthetic_circuit_hard_disables")
            logger.warning(
                "synthetic_circuit_hard_disabled",
                session_id=session_id,
                total_failures=state.total_failures,
                max_total=max_total,
            )
        elif state.consecutive_failures >= max_consecutive:
            state.disabled_until = time.monotonic() + cooldown_seconds
            record_metric("synthetic_circuit_opens")
            logger.warning(
                "synthetic_circuit_opened",
                session_id=session_id,
                consecutive_failures=state.consecutive_failures,
                cooldown_seconds=cooldown_seconds,
                total_failures=state.total_failures,
            )


def record_synthetic_success(session_id: str) -> None:
    """Record a successful synthetic tool call — resets consecutive counter."""
    from archolith_proxy.metrics import record_metric
    record_metric("synthetic_tool_successes")

    with _sessions_lock:
        state = get_circuit_state(session_id)
        state.consecutive_failures = 0


def reset_circuit(session_id: str) -> None:
    """Manually reset circuit state for a session."""
    with _sessions_lock:
        if session_id in _sessions:
            del _sessions[session_id]


def reset_all() -> None:
    """Reset all circuit state (for testing)."""
    with _sessions_lock:
        _sessions.clear()


def get_all_circuit_states() -> dict[str, dict]:
    """Return serialisable snapshot of all circuit states for /metrics."""
    with _sessions_lock:
        now = time.monotonic()
        result = {}
        for sid, state in _sessions.items():
            result[sid] = {
                "consecutive_failures": state.consecutive_failures,
                "total_failures": state.total_failures,
                "hard_disabled": state.hard_disabled,
                "total_input_tokens": state.total_input_tokens,
                "cooldown_remaining_s": max(0.0, state.disabled_until - now) if state.disabled_until > 0 else 0.0,
            }
        return result


# ── Per-session token budget ──────────────────────────────────────────────────

def add_session_tokens(session_id: str, tokens: int) -> None:
    """Accumulate input tokens for a session."""
    with _sessions_lock:
        state = get_circuit_state(session_id)
        state.total_input_tokens += tokens


def is_session_over_budget(session_id: str, max_tokens: int) -> bool:
    """Check if a session has exceeded its token budget.

    Returns True if max_tokens > 0 and the session has exceeded it.
    Returns False if max_tokens == 0 (unlimited).
    """
    if max_tokens <= 0:
        return False
    with _sessions_lock:
        state = get_circuit_state(session_id)
        return state.total_input_tokens >= max_tokens
