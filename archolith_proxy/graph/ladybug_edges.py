"""LadybugDB edge and decision operations."""

from __future__ import annotations

from uuid import uuid4


async def create_belongs_to(execute, session_id: str, fact_id: str) -> None:
    await execute(
        "MATCH (s:Session {session_id: $session_id}) MATCH (f:Fact {fact_id: $fact_id}) MERGE (f)-[:BELONGS_TO]->(s)",
        {"session_id": session_id, "fact_id": fact_id},
    )


async def bulk_create_touches(execute, session_id: str, touches: list[dict]) -> None:
    """Batch create/update file touches.

    Ladybug's current primary-key semantics reject the UNWIND+MERGE shape we
    use on Neo4j-style backends, so keep the public bulk API but execute the
    compatible single-row writes here.
    """
    for touch in touches:
        await create_touches(
            execute,
            session_id,
            touch["file_path"],
            touch["status"],
            touch["turn"],
        )


async def create_touches(execute, session_id: str, file_path: str, status: str, turn: int) -> None:
    existing = await execute(
        "MATCH (f:File {path: $path, session_id: $session_id}) RETURN f.file_id",
        {"path": file_path, "session_id": session_id},
    )
    if existing:
        fid = existing[0]["f.file_id"]
        sets = []
        params = {"fid": fid}
        sets.append("f.status = $status")
        params["status"] = status
        if status in ("modified", "created", "deleted"):
            sets.append("f.last_modified_turn = $turn")
            params["turn"] = turn
        if status == "read":
            sets.append("f.last_read_turn = $turn")
            params["turn"] = turn
        await execute(
            f"MATCH (f:File {{file_id: $fid}}) SET {', '.join(sets)}", params,
        )
    else:
        fid = "fl" + uuid4().hex[:14]
        await execute(
            """
            CREATE (f:File {
                file_id: $fid, path: $path, session_id: $session_id,
                status: $status, last_modified_turn: $lmt, last_read_turn: $lrt
            })
            """,
            {"fid": fid, "path": file_path, "session_id": session_id, "status": status,
             "lmt": turn if status in ("modified", "created", "deleted") else 0,
             "lrt": turn if status == "read" else 0},
        )
    await execute(
        "MATCH (s:Session {session_id: $session_id}) MATCH (f:File {path: $path, session_id: $session_id}) MERGE (s)-[:TOUCHES]->(f)",
        {"session_id": session_id, "path": file_path},
    )


async def create_supersedes(execute, old_fact_id: str, new_fact_id: str) -> None:
    await execute(
        "MATCH (old:Fact {fact_id: $old_id}) MATCH (new:Fact {fact_id: $new_id}) MERGE (new)-[:SUPERSEDES]->(old)",
        {"old_id": old_fact_id, "new_id": new_fact_id},
    )


async def bulk_create_supersedes(execute, pairs: list[tuple[str, str]]) -> None:
    """Batch-create SUPERSEDES edges with Ladybug-compatible writes."""
    for old_id, new_id in pairs:
        await create_supersedes(execute, old_id, new_id)


async def get_touched_files(execute, session_id: str) -> list[dict]:
    return await execute(
        """
        MATCH (s:Session {session_id: $session_id})-[:TOUCHES]->(f:File)
        RETURN f.path AS path, f.status AS status,
               f.last_modified_turn AS last_modified_turn,
               f.last_read_turn AS last_read_turn
        ORDER BY f.last_modified_turn DESC, f.last_read_turn DESC
        """,
        {"session_id": session_id},
    )


async def bulk_store_decisions(execute, session_id: str, decisions: list[dict], turn: int) -> list[str]:
    """Batch-create decisions with Ladybug-compatible writes."""
    decision_ids = []
    for decision in decisions:
        decision_ids.append(
            await store_decision(
                execute,
                session_id,
                decision.get("summary", ""),
                decision.get("rationale"),
                turn,
            )
        )
    return decision_ids


async def store_decision(execute, session_id: str, summary: str, rationale: str | None, turn: int) -> str:
    decision_id = "d" + uuid4().hex[:15]
    await execute(
        """
        CREATE (d:Decision {
            decision_id: $decision_id, session_id: $session_id,
            summary: $summary, rationale: $rationale,
            turn: $turn, superseded_by: NULL
        })
        """,
        {"decision_id": decision_id, "session_id": session_id, "summary": summary, "rationale": rationale, "turn": turn},
    )
    await execute(
        "MATCH (d:Decision {decision_id: $decision_id}) MATCH (s:Session {session_id: $session_id}) MERGE (d)-[:DECIDED_IN]->(s)",
        {"decision_id": decision_id, "session_id": session_id},
    )
    return decision_id


async def get_decisions(execute, session_id: str, include_superseded: bool = False) -> list[dict]:
    filter_clause = "" if include_superseded else "WHERE d.superseded_by IS NULL"
    return await execute(
        f"""
        MATCH (d:Decision {{session_id: $session_id}}) {filter_clause}
        RETURN d.decision_id AS decision_id, d.summary AS summary,
               d.rationale AS rationale, d.turn AS turn,
               d.superseded_by AS superseded_by
        ORDER BY d.turn ASC
        """,
        {"session_id": session_id},
    )
