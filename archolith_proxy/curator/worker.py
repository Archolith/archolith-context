"""Event-driven curator worker — long-lived per-session enrichment loop.

Phase 1 of the event-driven curator-worker plan. Replaces the request-coupled
prepper scheduling (extraction tail-task + cancel-on-next-turn) with a durable,
interrupt-driven worker:

- One long-lived asyncio task per active session, fed by an ordered event queue.
- **Idle-gated**: the loop blocks on an empty queue (no maintenance without
  pending events) and the registry evicts workers after an inactivity TTL.
- **Debounced**: a burst of events is coalesced into a single enrichment pass
  targeting the latest event (older events in the burst are superseded).
- **No cancel-and-lose**: a new event never cancels an in-flight pass; it simply
  queues and is picked up after the current pass completes. This is the key
  difference from ``swap_background_task``, which killed the in-flight prepper on
  every new turn and is why the prepper rarely produced a briefing.

The enrichment pass itself reuses the existing ``run_background_pass`` entry
point (which dispatches to the registered prepper), so this module changes the
*scheduling*, not the curation logic. Additive and flag-gated
(``curator_worker_enabled``); when the flag is off nothing here runs.

In-memory only — consistent with the rest of curator state. Durability (WAL) is
a later phase. Safe under a single asyncio event loop.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import dataclass, field
from uuid import uuid4

import structlog

logger = structlog.get_logger()


# ── Events ──────────────────────────────────────────────────────────────────


@dataclass
class SessionEvent:
    """An interrupt fed to a session's curator worker.

    ``kind="turn"`` requests an enrichment pass for the given turn.
    ``kind="session_end"`` asks the worker to stop after draining.
    """

    session_id: str
    turn_number: int = 0
    user_message: str = ""
    session_goal: str | None = None
    messages: list[dict] = field(default_factory=list)
    kind: str = "turn"  # "turn" | "session_end"
    created_at: float = field(default_factory=time.monotonic)


# ── Worker ──────────────────────────────────────────────────────────────────


class CuratorWorker:
    """A single session's long-lived enrichment loop.

    Owns an ordered, bounded event queue and one asyncio task that drains it.
    The task sleeps while the queue is empty (idle-gate) and is never interrupted
    mid-pass by a newer event.
    """

    def __init__(
        self,
        session_id: str,
        *,
        debounce_ms: int = 2000,
        max_queue: int = 100,
    ) -> None:
        self.session_id = session_id
        self._debounce_s = max(0.0, debounce_ms / 1000)
        self._queue: asyncio.Queue[SessionEvent] = asyncio.Queue(maxsize=max(1, max_queue))
        self._task: asyncio.Task | None = None
        self._last_active = time.monotonic()
        self.passes_run = 0
        self.events_coalesced = 0

    # -- lifecycle --

    def start(self) -> None:
        """Spawn the drain loop if it is not already running."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"curator-worker:{self.session_id}")

    @property
    def last_active(self) -> float:
        return self._last_active

    def enqueue(self, event: SessionEvent) -> None:
        """Append an event, coalescing under backpressure.

        Never blocks. When the queue is full the oldest pending event is dropped
        in favour of the newest — only the latest target matters, so dropping
        stale superseded events is correct.
        """
        self._last_active = time.monotonic()
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def aclose(self) -> None:
        """Cancel the drain loop and wait for it to unwind."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("curator_worker_close_error", session_id=self.session_id, exc_info=True)
        self._task = None

    # -- drain loop --

    async def _run(self) -> None:
        """Drain events forever: block when idle, debounce bursts, run one pass."""
        while True:
            event = await self._queue.get()  # idle-gate: sleeps until an event arrives
            if event.kind == "session_end":
                return

            # Debounce: let a burst accumulate, then collapse to the latest event.
            if self._debounce_s:
                await asyncio.sleep(self._debounce_s)
            latest = self._drain_to_latest(event)
            if latest.kind == "session_end":
                return

            # Run exactly one enrichment pass. New events that arrive while this
            # runs stay queued — the in-flight pass is NEVER cancelled.
            try:
                await self._run_pass(latest)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "curator_worker_pass_failed",
                    session_id=self.session_id,
                    turn=latest.turn_number,
                    exc_info=True,
                )

    def _drain_to_latest(self, first: SessionEvent) -> SessionEvent:
        """Collapse all currently-queued events into the most recent one.

        Stops early on a session_end so shutdown is honoured promptly.
        """
        latest = first
        while not self._queue.empty():
            try:
                nxt = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.events_coalesced += 1
            latest = nxt
            if nxt.kind == "session_end":
                break
        return latest

    async def _run_pass(self, event: SessionEvent) -> None:
        """Run a single background enrichment pass for ``event``."""
        from archolith_proxy.curator.pipeline import run_background_pass

        self._last_active = time.monotonic()
        await run_background_pass(
            session_id=event.session_id,
            turn_number=event.turn_number,
            user_message=event.user_message,
            session_goal=event.session_goal,
            messages=event.messages,
        )
        self.passes_run += 1
        self._last_active = time.monotonic()


# ── Registry ────────────────────────────────────────────────────────────────


class WorkerRegistry:
    """Owns one CuratorWorker per active session and their lifecycle.

    Optional single-leader leasing (archolith-maintenance ``SchedulerLeaseStore``):
    when a ``lease_store`` is injected, the registry only runs workers while it
    holds a process-wide lease, so two proxy processes sharing a session graph
    don't both run the curator worker (de-risks multi-writer). When no lease store
    is given, the registry is always the leader (behavior unchanged).
    """

    def __init__(
        self,
        *,
        debounce_ms: int = 2000,
        max_queue: int = 100,
        lease_store=None,
        lease_name: str = "curator-worker",
        lease_duration_s: float = 90.0,
    ) -> None:
        self._workers: dict[str, CuratorWorker] = {}
        self._debounce_ms = debounce_ms
        self._max_queue = max_queue
        # Single-leader leasing (optional).
        self._lease_store = lease_store
        self._lease_name = lease_name
        self._lease_duration_s = max(2.0, lease_duration_s)
        self._owner_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"
        self._owner_pid = os.getpid()
        self._lease_held = False
        self._last_lease_op = 0.0

    def _is_leader(self) -> bool:
        """True when this process may run workers. Cheap: re-checks the lease at
        most once per ~third of the lease duration."""
        if self._lease_store is None:
            return True
        now = time.monotonic()
        if self._lease_held and (now - self._last_lease_op) < (self._lease_duration_s / 3.0):
            return True
        try:
            held = self._lease_store.try_acquire(
                lease_name=self._lease_name, owner_id=self._owner_id,
                owner_pid=self._owner_pid, lease_duration_s=self._lease_duration_s,
            )
        except Exception:
            logger.warning("curator_worker_lease_error", exc_info=True)
            held = self._lease_held  # fail to last-known state; don't thrash
        self._lease_held = held
        self._last_lease_op = now
        try:
            from archolith_proxy.metrics import record_metric
            record_metric("curator_worker_lease_held" if held else "curator_worker_lease_blocked", 1)
        except Exception:
            pass
        return held

    def enqueue(self, event: SessionEvent) -> None:
        """Route an event to its session worker, spawning one on first use."""
        if not self._is_leader():
            return  # another process is the leader; do not run workers here
        worker = self._workers.get(event.session_id)
        if worker is None:
            worker = CuratorWorker(
                event.session_id,
                debounce_ms=self._debounce_ms,
                max_queue=self._max_queue,
            )
            self._workers[event.session_id] = worker
            worker.start()
        worker.enqueue(event)

    @property
    def active_count(self) -> int:
        return len(self._workers)

    async def shutdown_idle(self, ttl_s: float) -> int:
        """Evict workers with no activity for longer than ``ttl_s``. Returns count.

        Also renews the single-leader lease (if leasing is enabled) — this runs on
        the proxy's periodic cleanup tick.
        """
        if self._lease_store is not None and self._lease_held:
            try:
                renewed = self._lease_store.renew(
                    lease_name=self._lease_name, owner_id=self._owner_id,
                    owner_pid=self._owner_pid, lease_duration_s=self._lease_duration_s,
                )
                self._lease_held = renewed
                self._last_lease_op = time.monotonic()
            except Exception:
                logger.warning("curator_worker_lease_renew_error", exc_info=True)
        now = time.monotonic()
        stale = [sid for sid, w in self._workers.items() if now - w.last_active > ttl_s]
        for sid in stale:
            worker = self._workers.pop(sid, None)
            if worker is not None:
                await worker.aclose()
        if stale:
            logger.debug("curator_workers_evicted", count=len(stale))
        return len(stale)

    async def shutdown_session(self, session_id: str) -> None:
        """Stop and drop a single session's worker (e.g. on session end)."""
        worker = self._workers.pop(session_id, None)
        if worker is not None:
            await worker.aclose()

    async def shutdown_all(self) -> None:
        """Stop every worker — called on proxy shutdown."""
        workers = list(self._workers.values())
        self._workers.clear()
        for worker in workers:
            await worker.aclose()
        # Release the single-leader lease so another process can take over promptly.
        if self._lease_store is not None and self._lease_held:
            try:
                self._lease_store.release(lease_name=self._lease_name, owner_id=self._owner_id)
            except Exception:
                logger.debug("curator_worker_lease_release_error", exc_info=True)
            self._lease_held = False


# ── Module-level singleton + helpers ────────────────────────────────────────

_registry: WorkerRegistry | None = None


def get_worker_registry() -> WorkerRegistry:
    """Return the process-wide worker registry, building it from settings once."""
    global _registry
    if _registry is None:
        from archolith_proxy.config import get_settings

        settings = get_settings()
        lease_store = None
        if getattr(settings, "curator_worker_lease_enabled", False):
            try:
                from pathlib import Path

                from archolith_maintenance import SchedulerLeaseStore

                _db = getattr(settings, "curator_worker_lease_db_path", "") \
                    or str(Path(getattr(settings, "ladybug_db_path", "./data/context.lbug")).parent / "scheduler_leases.db")
                lease_store = SchedulerLeaseStore(db_path=Path(_db))
            except Exception:
                logger.warning("curator_worker_lease_store_init_failed", exc_info=True)
                lease_store = None
        _registry = WorkerRegistry(
            debounce_ms=getattr(settings, "curator_worker_debounce_ms", 2000),
            max_queue=getattr(settings, "curator_worker_max_queue", 100),
            lease_store=lease_store,
            lease_duration_s=getattr(settings, "curator_worker_lease_duration_s", 90.0),
        )
    return _registry


def enqueue_curator_event(
    session_id: str,
    turn_number: int,
    user_message: str,
    session_goal: str | None,
    messages: list[dict],
    kind: str = "turn",
) -> None:
    """Build a SessionEvent and hand it to the session's worker. Non-blocking."""
    get_worker_registry().enqueue(
        SessionEvent(
            session_id=session_id,
            turn_number=turn_number,
            user_message=user_message,
            session_goal=session_goal,
            messages=messages,
            kind=kind,
        )
    )


async def shutdown_idle_curator_workers(ttl_s: float) -> int:
    """Evict idle session workers; returns how many were stopped."""
    if _registry is None:
        return 0
    return await _registry.shutdown_idle(ttl_s)


async def shutdown_all_curator_workers() -> None:
    """Stop all session workers and reset the registry (proxy shutdown)."""
    global _registry
    if _registry is not None:
        await _registry.shutdown_all()
        _registry = None


__all__ = [
    "SessionEvent",
    "CuratorWorker",
    "WorkerRegistry",
    "get_worker_registry",
    "enqueue_curator_event",
    "shutdown_idle_curator_workers",
    "shutdown_all_curator_workers",
]
