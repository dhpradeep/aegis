"""Evaluator: a separate LLM grader that scores an agent's work against a rubric.

`Evaluator.grade` runs a fresh grader turn whose only job is to compare a
GOAL/RUBRIC pair against the primary agent's final OUTPUT and produced files,
and to return a structured verdict. The grader may use read-only tools to
inspect the actual files in the workspace before judging, and is given enough
turns to think, look, and then answer with a single JSON object.

`_parse_verdict` extracts and validates that JSON defensively (the model is
not guaranteed to follow the format), and `grade` retries a few times before
giving up — a parse/format failure is the evaluator's problem, flagged via
`"error": True`, never fed back to the agent as a fake "gap".
"""

from __future__ import annotations

import json
import re

from app.core.config import get_settings
from app.services.agent.runtime import AgentRuntime, RunConfig, run_and_collect

# Read-only tools so the grader can verify the produced files first-hand
# (seeing the manifest alone under-grades), without being able to mutate them.
GRADER_TOOLS = ["Read", "Glob", "Grep"]
# Enough turns for a thinking model to reason, optionally inspect a file, and
# then emit the verdict. `max_turns=1` caused `error_max_turns` (empty result)
# whenever the grader took a single tool step.
GRADER_MAX_TURNS = 8
# How many times to re-run the grader if it returns an unparseable/empty verdict.
GRADER_ATTEMPTS = 3

GRADER_SYSTEM_PROMPT = (
    "You are a strict evaluator. You are given a GOAL, a RUBRIC, the agent's "
    "final OUTPUT, and a list of files it produced. Your working directory is "
    "the agent's workspace, so you MAY use the read-only tools (Read, Glob, "
    "Grep) to inspect the actual files before judging. When you are done, your "
    "FINAL message must be ONLY a single JSON object and nothing else — no "
    "markdown fences, no prose before or after: "
    '{"satisfied": bool, "score": number between 0 and 1, "gaps": [string], '
    '"reasoning": string}. "gaps" lists concrete, actionable shortfalls (empty '
    "when satisfied)."
)


class Evaluator:
    """Grades an agent's work against a rubric using a separate LLM call."""

    def __init__(self, runtime: AgentRuntime, model: str | None = None) -> None:
        self.runtime = runtime
        self.model = model

    async def grade(
        self,
        *,
        goal: str,
        rubric: str,
        artifact_text: str,
        file_manifest: list[dict],
        cwd: str,
    ) -> dict:
        prompt = (
            f"GOAL:\n{goal}\n\n"
            f"RUBRIC:\n{rubric}\n\n"
            f"OUTPUT:\n{artifact_text}\n\n"
            f"FILE MANIFEST:\n{json.dumps(file_manifest)}"
        )

        settings = get_settings()
        cfg = RunConfig(
            prompt=prompt,
            cwd=cwd,
            system_prompt=GRADER_SYSTEM_PROMPT,
            allowed_tools=GRADER_TOOLS,
            permission_mode="default",
            mcp_servers={},
            model=self.model or settings.default_model,
            max_turns=GRADER_MAX_TURNS,
            resume=None,
            timeout_s=settings.run_timeout_s,
            agents=None,
            effort=None,
        )

        verdict: dict = {}
        for _ in range(GRADER_ATTEMPTS):
            final = await run_and_collect(self.runtime, cfg, None)
            text = (final or {}).get("result") or ""
            verdict = _parse_verdict(text)
            if not verdict.get("error"):
                return verdict
        # All attempts failed to produce a parseable verdict.
        return verdict


def _parse_verdict(text: str) -> dict:
    """Extract and validate a verdict JSON object from grader output text.

    Returns a verdict dict with an `"error"` flag: False when a valid JSON
    verdict was parsed, True (with empty `gaps`) when it could not be — so the
    loop can distinguish an evaluator failure from real agent shortfalls.
    """
    if text:
        cleaned = re.sub(r"```(?:json)?|```", "", text).strip()
        data = None
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            match = re.search(r"\{.*\}", cleaned, re.S)
            if match:
                try:
                    data = json.loads(match.group(0))
                except (json.JSONDecodeError, ValueError):
                    data = None
        if isinstance(data, dict) and "satisfied" in data:
            return {
                "satisfied": bool(data.get("satisfied", False)),
                "score": float(data.get("score", 0.0)),
                "gaps": list(data.get("gaps", [])),
                "reasoning": str(data.get("reasoning", "")),
                "error": False,
            }

    return {
        "satisfied": False,
        "score": 0.0,
        "gaps": [],
        "reasoning": text[:500],
        "error": True,
    }
