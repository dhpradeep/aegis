"""The autonomous Objective loop engine: act -> LLM-grade -> iterate until
the `Evaluator` is satisfied or the objective's iteration/cost budget runs
out.

`run_objective` is scheduled as a background `asyncio.Task` by
`app.services.objectives.submit_objective` and, mirroring
`app.services.jobs.run_job`, opens its own `SessionLocal`s throughout (never
a request-scoped session) since it keeps running long after the request
that triggered it has returned. Each iteration:

- **ACT**: runs one turn via `build_run_config` + `run_session_message`
  (Task 12's runner), which persists the turn's own events/Usage and
  captures `sdk_session_id` on the working `Session`.
- **EVALUATE**: grades the turn's final result text + file manifest against
  the objective's goal/rubric via `Evaluator.grade` (or an injected fake).
- **DECIDE**: stops on a satisfied verdict, otherwise feeds the verdict's
  gaps back into the next turn's prompt.

`objective.iteration_started` / `objective.evaluation` / `objective.finished`
marker `Event`s are persisted on the working session alongside the driven
session's own events, with `seq` continuing from the session's existing max
(no separate event table for objectives).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.models import Event, Objective, Session
from app.db.models._base import _utcnow
from app.services.agent.evaluator import Evaluator
from app.services.agent.session_runner import build_run_config, run_session_message
from app.services.jobs import fire_webhook
from app.services.sessions import create_session_record
from app.services.workspaces import list_files

logger = logging.getLogger("app.objectives")

__all__ = ["run_objective"]

# Injected into every objective run's system prompt: the loop has no human, so
# the agent must decide for itself rather than ask, and must not burn a turn
# re-verifying a confirmed blocker.
AUTONOMY_DIRECTIVE = (
    "\n\n---\n"
    "You are running fully autonomously inside an isolated, disposable workspace "
    "as one iteration of a goal-completion loop. There is NO human available to "
    "answer questions or approve actions. Never ask clarifying questions and never "
    "wait for input — make the most reasonable assumption, state it in one line, and "
    "proceed to complete the goal. Do all work inside the current working directory. "
    "If a capability is genuinely blocked (a command is denied), do NOT retry it "
    "repeatedly or spend the turn re-verifying the block — state the exact blocker "
    "once and accomplish everything else you can."
)

# Substrings that mark a tool result as a permission/approval denial rather than
# a normal tool error. Matched case-insensitively against tool_result content.
_PERMISSION_MARKERS = (
    "requires approval",
    "require approval",
    "requires permission",
    "requires manual approval",
    "can execute untrusted",
    "blocked. for security",
    "blocked for security",
)


def _is_permission_denied(content: str) -> bool:
    low = content.lower()
    return any(m in low for m in _PERMISSION_MARKERS)


async def _next_seq(db, session_id: str) -> int:
    result = await db.execute(select(func.max(Event.seq)).where(Event.session_id == session_id))
    return result.scalar() or 0


async def run_objective(app: Any, objective_id: str, *, evaluator=None) -> None:
    """Drive `objective_id`'s working session through the act/grade/iterate
    loop until it succeeds or exhausts its iteration/cost budget.

    On any unhandled exception, best-effort marks the objective "failed"
    (still firing the tenant's webhook) rather than leaving it stuck
    "running" forever.
    """
    try:
        async with SessionLocal() as db:
            obj = await db.get(Objective, objective_id)
            if obj is None:
                return
            obj.status = "running"

            session = await create_session_record(
                db,
                tenant_id=obj.tenant_id,
                agent_id=obj.agent_id,
                allow_admin_only=True,
                title=f"objective {obj.id}",
            )
            obj.session_id = session.id
            tenant_id = obj.tenant_id
            api_key_id = obj.api_key_id
            await db.commit()

        session_id = session.id
        runtime = app.state.runtime
        grader = evaluator or Evaluator(runtime)

        prompt = obj.goal
        result_text = ""
        succeeded = False
        terminal_reason: str | None = None

        while True:
            async with SessionLocal() as db:
                obj = await db.get(Objective, objective_id)
                if obj.iterations_done >= obj.max_iterations:
                    terminal_reason = "max_iterations"
                    break
                if obj.max_cost_usd is not None and obj.cost_usd >= obj.max_cost_usd:
                    terminal_reason = "budget_exhausted"
                    break

                obj.iterations_done += 1
                # `run_session_message` (called below, on its own SessionLocal)
                # also writes Events against this session and derives its own
                # seq from the current max, so re-derive ours fresh here too
                # rather than tracking a local counter that would go stale
                # across that call.
                seq = await _next_seq(db, session_id) + 1
                db.add(
                    Event(
                        session_id=session_id,
                        seq=seq,
                        type="objective.iteration_started",
                        payload_json=json.dumps({"iteration": obj.iterations_done}),
                    )
                )
                session_row = await db.get(Session, session_id)
                cfg = await build_run_config(
                    db, obj.tenant_id, session_row, prompt, system_suffix=AUTONOMY_DIRECTIVE
                )
                await db.commit()

            # ACT: drive one turn, collecting the final result event and
            # watching for permission/approval denials so a blocked run can
            # stop with a precise reason instead of grinding more iterations.
            final_result: dict | None = None
            tool_cmds: dict[str, str] = {}
            denied: list[str] = []
            async for ev in run_session_message(
                runtime,
                cfg,
                tenant_id=tenant_id,
                api_key_id=api_key_id,
                session_id=session_id,
            ):
                etype = ev.get("type")
                if etype == "tool_use":
                    inp = ev.get("input") if isinstance(ev.get("input"), dict) else {}
                    label = inp.get("command") or inp.get("description") or ev.get("name") or ""
                    tool_cmds[ev.get("id", "")] = label
                elif etype == "tool_result" and ev.get("is_error"):
                    if _is_permission_denied(str(ev.get("content") or "")):
                        label = tool_cmds.get(ev.get("tool_use_id", ""), "") or "a command"
                        if label not in denied:
                            denied.append(label)
                elif etype == "result":
                    final_result = ev

            cost_delta = 0.0
            if final_result is not None:
                result_text = final_result.get("result") or ""
                cost_delta = final_result.get("cost_usd") or 0.0

            async with SessionLocal() as db:
                # Re-fetch: run_session_message updated sdk_session_id/status
                # on its own SessionLocal.
                session_row = await db.get(Session, session_id)
                obj = await db.get(Objective, objective_id)
                obj.cost_usd += cost_delta
                await db.commit()
                workspace_path = session_row.workspace_path
                goal = obj.goal
                rubric = obj.rubric

            # EVALUATE
            manifest = list_files(Path(workspace_path))
            verdict = await grader.grade(
                goal=goal,
                rubric=rubric,
                artifact_text=result_text,
                file_manifest=manifest,
                cwd=workspace_path,
            )

            async with SessionLocal() as db:
                seq = await _next_seq(db, session_id) + 1
                db.add(
                    Event(
                        session_id=session_id,
                        seq=seq,
                        type="objective.evaluation",
                        payload_json=json.dumps(verdict),
                    )
                )
                await db.commit()

            # DECIDE
            if verdict.get("satisfied"):
                succeeded = True
                break

            # The evaluator couldn't produce a valid grade (after its own
            # retries). That's our infra problem, not an agent shortfall — stop
            # honestly instead of feeding a phantom gap back to the agent.
            if verdict.get("error"):
                terminal_reason = "evaluation_error"
                result_text = (
                    result_text
                    + "\n\n[loop] The evaluator could not produce a valid verdict "
                    "after several attempts; stopping without a conclusive grade."
                ).strip()
                break

            # If the turn was blocked by the permission gate, stop now with a
            # precise reason rather than iterating into the same wall.
            if denied:
                block_reason = (
                    "Stopped: blocked by the permission gate. This run needs to execute "
                    "commands that require approval, but an autonomous objective has no "
                    "approver. Denied:\n- " + "\n- ".join(denied) + "\n\n"
                    "To allow these, enable “Bypass permissions” on this agent "
                    "(admin-only) so it can run commands without approval."
                )
                result_text = (result_text + "\n\n" + block_reason).strip()
                terminal_reason = "permission_blocked"
                async with SessionLocal() as db:
                    seq = await _next_seq(db, session_id) + 1
                    db.add(
                        Event(
                            session_id=session_id,
                            seq=seq,
                            type="objective.blocked",
                            payload_json=json.dumps({"denied": denied, "reason": block_reason}),
                        )
                    )
                    await db.commit()
                break

            gaps = verdict.get("gaps") or []
            prompt = (
                "Not done yet. You are autonomous — do not ask questions; make reasonable "
                "assumptions and proceed. Address these gaps and continue:\n- "
                + "\n- ".join(gaps)
            )

        async with SessionLocal() as db:
            obj = await db.get(Objective, objective_id)
            if succeeded:
                status = "succeeded"
            else:
                status = terminal_reason or "max_iterations"

            obj.status = status
            obj.result_text = result_text
            obj.finished_at = _utcnow()
            seq = await _next_seq(db, session_id) + 1
            db.add(
                Event(
                    session_id=session_id,
                    seq=seq,
                    type="objective.finished",
                    payload_json=json.dumps({"status": status}),
                )
            )
            await db.commit()

            await fire_webhook(
                db,
                obj.tenant_id,
                {"type": f"objective.{status}", "objective_id": obj.id, "result": obj.result_text},
            )

    except Exception:
        logger.exception("objective %s failed", objective_id)
        try:
            async with SessionLocal() as db:
                obj = await db.get(Objective, objective_id)
                if obj is not None:
                    obj.status = "failed"
                    obj.finished_at = _utcnow()
                    await db.commit()
                    await fire_webhook(
                        db,
                        obj.tenant_id,
                        {
                            "type": "objective.failed",
                            "objective_id": obj.id,
                            "result": obj.result_text,
                        },
                    )
        except Exception:
            logger.exception("objective %s: failed to persist failure status", objective_id)
