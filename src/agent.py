"""
Multi-Agent Orchestration with Resonate

An orchestrator coordinates three specialist AI agents in sequence:
researcher -> writer -> reviewer. Each agent handoff is a durable
checkpoint via `await ctx.run(...)`. If any agent fails (API timeout,
crash, rate limit), Resonate retries that step. Completed agents are NOT
re-run -- their output is cached at the checkpoint.

Human-in-the-loop is a natural extension: replace the simulated approval
with `await ctx.promise()` and resolve it externally. The workflow
blocks until the promise is resolved and survives restarts while waiting.
"""

import asyncio
import os
import sys
import time
from typing import TYPE_CHECKING, TypedDict

from dotenv import load_dotenv
from openai import OpenAI
from resonate.resonate import Resonate

if TYPE_CHECKING:
    from resonate.context import Context

load_dotenv()

url = os.environ.get("RESONATE_URL", "http://localhost:8001")
resonate = Resonate(url=url)


class OrchestrationResult(TypedDict):
    status: str
    topic: str
    findings: str
    draft: str
    review: str


# Track agent attempts for the crash demo. Lives in worker memory; the
# durable promise store remembers which step succeeded, so on retry only
# the failed step re-enters this map.
_agent_attempts: dict[str, int] = {}


# ============================================================================
# Researcher Agent
# ============================================================================
# Researches a topic and returns a structured set of findings.
# In a production system this would call search APIs, RAG databases, etc.


async def researcher(ctx: "Context", topic: str) -> str:
    print(f'[researcher]  Researching: "{topic}"...')
    client: OpenAI = ctx.get_dependency(OpenAI)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_completion_tokens=400,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a research specialist. Given a topic, provide 3-5 concise "
                    "key findings that would be useful for writing an article. Format "
                    "as a numbered list. Be factual and concise."
                ),
            },
            {"role": "user", "content": f"Research this topic: {topic}"},
        ],
    )

    findings = response.choices[0].message.content or ""
    print(f"[researcher]  Complete ({len(findings)} chars)")
    return findings


# ============================================================================
# Writer Agent
# ============================================================================
# Takes research findings and writes a short article draft.
# Simulates a failure on first attempt in crash demo mode.


async def writer(ctx: "Context", topic: str, research: str, crash_on_first: bool) -> str:
    key = f"writer:{topic}"
    _agent_attempts[key] = _agent_attempts.get(key, 0) + 1
    attempt = _agent_attempts[key]

    print(f"[writer]      Writing article (attempt {attempt})...")

    if crash_on_first and attempt == 1:
        await asyncio.sleep(0.2)
        raise RuntimeError("Writer agent connection reset (simulated)")

    client: OpenAI = ctx.get_dependency(OpenAI)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_completion_tokens=500,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a professional writer. Write a concise 2-paragraph "
                    "article based on the provided research. Use clear, engaging "
                    "language. Include a title."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Topic: {topic}\n\nResearch findings:\n{research}\n\n"
                    "Write a short article."
                ),
            },
        ],
    )

    draft = response.choices[0].message.content or ""
    print(f"[writer]      Complete ({len(draft)} chars)")
    return draft


# ============================================================================
# Reviewer Agent
# ============================================================================
# Reviews the draft and provides a brief approval decision.


async def reviewer(ctx: "Context", draft: str) -> str:
    print("[reviewer]    Reviewing draft...")
    client: OpenAI = ctx.get_dependency(OpenAI)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_completion_tokens=200,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an editorial reviewer. Review the provided article "
                    "draft. Give a brief approval decision (APPROVED or "
                    "NEEDS_REVISION) with 1-2 sentences of reasoning."
                ),
            },
            {"role": "user", "content": f"Review this article:\n\n{draft}"},
        ],
    )

    review = response.choices[0].message.content or ""
    print(f"[reviewer]    {review[:60]}...")
    return review


# ============================================================================
# Orchestrator Workflow
# ============================================================================
# An orchestrator delegates to three specialist agents in sequence.
# Each `await ctx.run(...)` is a durable checkpoint -- if any agent fails,
# Resonate retries that step only. Completed agents are NOT re-run.


async def orchestrate(ctx: "Context", topic: str, crash_on_writer: bool = False):
    # Step 1: Research -- gather findings
    findings = await ctx.run(researcher, topic)

    # Step 2: Write -- produce a draft from findings
    # If crash_on_writer=True, the writer fails on first attempt and retries.
    # The researcher does NOT re-run on retry -- its result is cached.
    draft = await ctx.run(writer, topic, findings, crash_on_writer)

    # Step 3: Review -- check the draft quality
    review = await ctx.run(reviewer, draft)

    # Step 4: Human approval (simulated in this demo).
    # In production:
    #   approval = await ctx.promise()
    #   approved = await approval
    # The workflow blocks until the promise is resolved externally and
    # survives restarts while waiting -- the promise IS the checkpoint.
    approved = "APPROVED" in review.upper()

    result: OrchestrationResult = {
        "status": "published" if approved else "rejected",
        "topic": topic,
        "findings": findings,
        "draft": draft,
        "review": review,
    }
    return result


# ============================================================================
# Entry Point
# ============================================================================


async def _main() -> None:
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        raise SystemExit("OPENAI_API_KEY environment variable is required")

    resonate.with_dependency(OpenAI(api_key=openai_api_key))
    resonate.register(orchestrate)

    # CLI flags for ad-hoc demoing without going through `resonate invoke`.
    crash_mode = "--crash" in sys.argv
    topic = "The future of durable execution in AI applications"
    for arg in sys.argv:
        if arg.startswith("--topic="):
            topic = arg[len("--topic="):]

    print("=== Resonate Multi-Agent Orchestration ===")
    mode = "CRASH (writer agent fails on first attempt)" if crash_mode else "HAPPY PATH"
    print(f"Mode: {mode}")
    print(f'Topic: "{topic}"\n')
    print("Pipeline: researcher -> writer -> reviewer -> [human approval] -> publish\n")

    print("[worker]     starting — registered: orchestrate")
    print("[worker]     waiting for work from the Resonate Server...")
    print("\nInvoke with:")
    print(
        '   resonate invoke orchestration.1 --func orchestrate '
        f'--arg "{topic}" --arg {str(crash_mode).lower()}'
    )
    print()

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nShutting down...")
    finally:
        await resonate.stop()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
