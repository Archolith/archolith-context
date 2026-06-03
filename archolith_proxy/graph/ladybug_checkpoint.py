"""LadybugDB checkpoint, issue, and verification operations."""

from __future__ import annotations

from uuid import uuid4


async def upsert_checkpoint(execute, session_id: str, summary: str, next_step: str, confidence: float, turn: int) -> None:
    existing = await execute(
        "MATCH (c:Checkpoint {session_id: $session_id}) RETURN c.session_id AS sid",
        {"session_id": session_id},
    )
    if existing:
        await execute(
            """
            MATCH (c:Checkpoint {session_id: $session_id})
            SET c.summary = $summary, c.next_step = $next_step,
                c.confidence = $confidence, c.source_turn = $turn,
                c.updated_at = current_timestamp()
            """,
            {"session_id": session_id, "summary": summary, "next_step": next_step,
             "confidence": confidence, "turn": turn},
        )
    else:
        await execute(
            """
            CREATE (c:Checkpoint {
                session_id: $session_id, summary: $summary, next_step: $next_step,
                confidence: $confidence, source_turn: $turn,
                updated_at: current_timestamp()
            })
            """,
            {"session_id": session_id, "summary": summary, "next_step": next_step,
             "confidence": confidence, "turn": turn},
        )


async def get_checkpoint(execute, session_id: str) -> dict | None:
    rows = await execute(
        """
        MATCH (c:Checkpoint {session_id: $session_id})
        RETURN c.summary AS summary, c.next_step AS next_step,
               c.confidence AS confidence, c.source_turn AS source_turn
        """,
        {"session_id": session_id},
    )
    return rows[0] if rows else None


async def create_issue(execute, session_id: str, summary: str, status: str,
                       related_file: str, related_command: str, turn: int) -> None:
    iid = "iss" + uuid4().hex[:13]
    await execute(
        """
        CREATE (i:Issue {
            issue_id: $iid, session_id: $session_id, status: $status,
            summary: $summary, related_file: $related_file,
            related_command: $related_command, resolution_ref: '',
            source_turn: $turn, resolved_turn: 0,
            created_at: current_timestamp()
        })
        """,
        {"iid": iid, "session_id": session_id, "status": status,
         "summary": summary, "related_file": related_file or "",
         "related_command": related_command or "", "turn": turn},
    )


async def bulk_create_issues(execute, session_id: str, issues: list[dict], turn: int) -> list[str]:
    """Batch-create issues with Ladybug-compatible writes."""
    issue_ids = []
    for issue in issues:
        iid = "iss" + uuid4().hex[:13]
        issue_ids.append(iid)
        await execute(
            """
            CREATE (i:Issue {
                issue_id: $iid, session_id: $session_id, status: $status,
                summary: $summary, related_file: $related_file,
                related_command: $related_command, resolution_ref: '',
                source_turn: $turn, resolved_turn: 0,
                created_at: current_timestamp()
            })
            """,
            {
                "iid": iid,
                "session_id": session_id,
                "status": issue.get("status", "open"),
                "summary": issue.get("summary", ""),
                "related_file": issue.get("related_file", "") or "",
                "related_command": issue.get("related_command", "") or "",
                "turn": turn,
            },
        )
    return issue_ids


async def resolve_issues(execute, session_id: str, summaries: list[str], resolution_ref: str, turn: int) -> None:
    for summary in summaries:
        await execute(
            """
            MATCH (i:Issue {session_id: $session_id, status: 'open', summary: $summary})
            SET i.status = 'resolved', i.resolution_ref = $ref, i.resolved_turn = $turn
            """,
            {"session_id": session_id, "summary": summary, "ref": resolution_ref, "turn": turn},
        )


async def bulk_resolve_issues(execute, session_id: str, summaries: list[str], resolution_ref: str, turn: int) -> None:
    """Batch-resolve issues with Ladybug-compatible writes."""
    for summary in summaries:
        await resolve_issues(execute, session_id, [summary], resolution_ref, turn)


async def get_open_issues(execute, session_id: str) -> list[dict]:
    return await execute(
        """
        MATCH (i:Issue {session_id: $session_id, status: 'open'})
        RETURN i.issue_id AS issue_id, i.summary AS summary,
               i.related_file AS related_file,
               i.related_command AS related_command,
               i.source_turn AS source_turn
        ORDER BY i.source_turn ASC
        """,
        {"session_id": session_id},
    )


async def create_verification(execute, session_id: str, command: str, status: str, summary: str, turn: int) -> None:
    vid = "ver" + uuid4().hex[:13]
    await execute(
        """
        CREATE (v:Verification {
            verification_id: $vid, session_id: $session_id,
            command: $command, status: $status, summary: $summary,
            source_turn: $turn, created_at: current_timestamp()
        })
        """,
        {"vid": vid, "session_id": session_id, "command": command,
         "status": status, "summary": summary, "turn": turn},
    )


async def bulk_create_verifications(execute, session_id: str, verifications: list[dict], turn: int) -> list[str]:
    """Batch-create verifications with Ladybug-compatible writes."""
    verification_ids = []
    for verification in verifications:
        vid = "ver" + uuid4().hex[:13]
        verification_ids.append(vid)
        await execute(
            """
            CREATE (v:Verification {
                verification_id: $vid, session_id: $session_id,
                command: $command, status: $status, summary: $summary,
                source_turn: $turn, created_at: current_timestamp()
            })
            """,
            {
                "vid": vid,
                "session_id": session_id,
                "command": verification.get("command", ""),
                "status": verification.get("status", "fail"),
                "summary": verification.get("summary", ""),
                "turn": turn,
            },
        )
    return verification_ids


async def get_last_verification(execute, session_id: str) -> dict | None:
    rows = await execute(
        """
        MATCH (v:Verification {session_id: $session_id})
        RETURN v.command AS command, v.status AS status,
               v.summary AS summary, v.source_turn AS source_turn
        ORDER BY v.source_turn DESC
        LIMIT 1
        """,
        {"session_id": session_id},
    )
    return rows[0] if rows else None
