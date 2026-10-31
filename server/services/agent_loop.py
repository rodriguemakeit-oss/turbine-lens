"""Turbine Lens_MVP agent loop — claude-agent-sdk runner.

Lifted and trimmed from databricks-builder-app `server/services/agent.py`.
Phase 0: proves the loop spawns and answers via Databricks FMAPI inside a
Databricks App. No MCP tools or skills are wired yet (added in Phase 1+).

Preserved from the source (the parts we de-risked):
  * Fresh event loop in a worker thread — works around claude-agent-sdk
    issue #462 (subprocess transport fails inside the uvicorn event loop).
  * env passed as {**os.environ, **claude_env} — replacing the env strips
    PATH/HOME and the bundled `claude` (#!/usr/bin/env node) can't find node.
  * include_partial_messages=True for token streaming.
  * FMAPI wiring: ANTHROPIC_BASE_URL/MODEL/AUTH + coding-agent-mode header
    + CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
from contextvars import copy_context
from pathlib import Path
from typing import AsyncIterator

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from . import skills_manager as _sm

from ..config import ALLOW_EDIT

logger = logging.getLogger(__name__)

CLAUDE_INTERNAL_TOOLS = ["Read", "Edit", "Bash", "Glob", "Grep", "Agent"] if ALLOW_EDIT else ["Read", "Agent"]
# Read-only posture: we deliberately grant NONE of Claude's built-in tools
# (Read / Edit / Bash / Glob / Grep / Agent). Charts go through the safe
# `render_chart` MCP tool — NOT agent-run code — so there is no need for a shell
# or filesystem access. Granting Bash/Edit under permission_mode="bypassPermissions"
# would let a prompt run arbitrary code on the app container (which holds the SP's
# credentials). Only our read-only MCP tools + the Skill tool are allowed.


def _sync_skills(cwd: str, skills_src: str | None) -> bool:
    """Copy skills into <cwd>/.claude/skills and provision agent definitions.

    Uses skills_manager so that:
      - ENABLED_SKILLS env var is respected (filter which skills are active).
      - Per-project enabled_skills.json overrides are honoured.
      - Agent definitions from application/.claude/agents/ are provisioned.
      - Both SKILL.md and SKILL.MD filename variants are supported.

    Returns True if at least one skill was synced.
    """
    if not skills_src or not os.path.isdir(skills_src):
        return False

    project_dir = Path(cwd)
    enabled_skills = _sm.get_project_enabled_skills(project_dir)
    ok = _sm.copy_skills_and_agents_to_project(project_dir, enabled_skills=enabled_skills)
    _sm.sync_project_agents(project_dir)
    return ok


def _build_claude_env(fmapi_host: str, fmapi_token: str, model: str) -> dict:
    """Build the env vars that point the Claude Code CLI at Databricks FMAPI."""
    host = fmapi_host.replace("https://", "").replace("http://", "").rstrip("/")

    # OTEL UC table prefix: <catalog>.<schema>.<prefix> — suffixes _metrics/_spans/_logs
    # are appended automatically. Default preserves the existing table names.
    otel_prefix = os.environ.get(
        "TURBINE_LENS_OTEL_TABLE_PREFIX", ""
    )

    env = {
        

        "ANTHROPIC_API_KEY": fmapi_token,
        "ANTHROPIC_AUTH_TOKEN": fmapi_token,
        "ANTHROPIC_MODEL": model,
        # Required for FMAPI compatibility (learned from the spawn test).
        "ANTHROPIC_CUSTOM_HEADERS": "x-databricks-use-coding-agent-mode: true",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        # 1h stream timeout to survive long tool sequences (Phase 1+).
        "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT": os.environ.get("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "3600000"),
        

        "ANTHROPIC_BASE_URL": f"https://{host}/ai-gateway/anthropic",
        "CLAUDE_CODE_USE_GATEWAY": "1",
        "ENABLE_PROMPT_CACHING_1H": "1",
        "ENABLE_TOOL_SEARCH": "1",

        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",

        # Metrics OTEL exporter config
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": f"https://{host}/api/2.0/otel/v1/metrics",
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS": f"content-type=application/x-protobuf,Authorization=Bearer {fmapi_token},X-Databricks-UC-Table-Name={otel_prefix}_metrics",
        "OTEL_METRIC_EXPORT_INTERVAL": "5000",

        "OTEL_LOG_USER_PROMPTS": "1",
        "OTEL_LOG_TOOL_DETAILS": "1",
        "OTEL_LOG_TOOL_CONTENT": "1",
        "CLAUDE_CODE_PROPAGATE_TRACEPARENT": "1",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"https://{host}/api/2.0/otel/v1/traces",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": f"content-type=application/x-protobuf,Authorization=Bearer {fmapi_token},X-Databricks-UC-Table-Name={otel_prefix}_spans",

        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": f"https://{host}/api/2.0/otel/v1/logs",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS": f"content-type=application/x-protobuf,Authorization=Bearer {fmapi_token},X-Databricks-UC-Table-Name={otel_prefix}_logs",

        "MLFLOW_ENABLE_DUAL_EXPORT": "true",
    }
    return env


def _run_agent_in_fresh_loop(message, options, result_queue, context):
    """Run the agent in a fresh event loop on a worker thread (issue #462)."""

    def run_with_context():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def run_client():
            try:
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(message)
                    async for msg in client.receive_response():
                        result_queue.put(("message", msg))
            except Exception as e:  # noqa: BLE001
                logger.exception("Agent query error: %s", e)
                result_queue.put(("error", e))
            finally:
                result_queue.put(("done", None))

        try:
            loop.run_until_complete(run_client())
        finally:
            loop.close()

    context.run(run_with_context)


def _process_tool_result(block: ToolResultBlock) -> dict:
    content = block.content
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                texts.append(item["text"])
            elif isinstance(item, str):
                texts.append(item)
            else:
                texts.append(str(item))
        content = "\n".join(texts) if texts else str(block.content)
    elif not isinstance(content, str):
        content = str(content)
    return {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": content,
        "is_error": block.is_error,
    }


async def stream_agent_response(
    message: str,
    system_prompt: str,
    fmapi_host: str,
    fmapi_token: str,
    model: str,
    *,
    session_id: str | None = None,
    working_dir: str | None = None,
    allowed_tools: list[str] | None = None,
    mcp_servers: dict | None = None,
    skills_dir: str | None = None,
) -> AsyncIterator[dict]:
    """Stream a Claude agent response as normalized event dicts.

    Phase 0 callers pass no tools/mcp_servers — this proves the loop. Phase 1+
    pass the lifted Databricks MCP server + allowed tool names.
    """
    cwd = working_dir or os.path.join(os.getcwd(), "_agent_workdir")
    os.makedirs(cwd, exist_ok=True)

    claude_env = _build_claude_env(fmapi_host, fmapi_token, model)

    # Skills: sync into the cwd and enable the Skill tool if present.
    tools_list = list(allowed_tools or [])
    setting_sources = None
    if _sync_skills(cwd, skills_dir):
        setting_sources = ["project"]
        if "Skill" not in tools_list:
            tools_list.append("Skill")    

    options = ClaudeAgentOptions(
        cwd=cwd,
        tools=tools_list + CLAUDE_INTERNAL_TOOLS,
        allowed_tools=tools_list + CLAUDE_INTERNAL_TOOLS,
        permission_mode="bypassPermissions",
        resume=session_id,
        mcp_servers=mcp_servers or {},
        system_prompt=system_prompt,
        setting_sources=setting_sources,
        # Merge over the real environment — see module docstring (#462 / node).
        env={**os.environ, **claude_env},
        include_partial_messages=True,
        max_buffer_size=10 * (1024**2),  # 10 MB
        thinking={"type": "adaptive", "display": "summarized"},
        effort="high",
    )

    ctx = copy_context()
    result_queue: queue.Queue = queue.Queue()
    thread = threading.Thread(
        target=_run_agent_in_fresh_loop,
        args=(message, options, result_queue, ctx),
        daemon=True,
    )
    thread.start()

    KEEPALIVE_INTERVAL = 15
    while True:

        def get_with_timeout():
            try:
                return result_queue.get(timeout=KEEPALIVE_INTERVAL)
            except queue.Empty:
                return ("keepalive", None)

        msg_type, msg = await asyncio.get_event_loop().run_in_executor(None, get_with_timeout)

        if msg_type == "keepalive":
            yield {"type": "keepalive"}
            continue
        if msg_type == "done":
            break
        if msg_type == "error":
            yield {"type": "error", "error": str(msg)}
            break

        # msg_type == "message"
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    yield {"type": "text", "text": block.text}
                elif isinstance(block, ThinkingBlock):
                    yield {"type": "thinking", "thinking": block.thinking}
                elif isinstance(block, ToolUseBlock):
                    yield {"type": "tool_use", "tool_id": block.id, "tool_name": block.name, "tool_input": block.input}
                elif isinstance(block, ToolResultBlock):
                    yield _process_tool_result(block)
        elif isinstance(msg, ResultMessage):
            yield {
                "type": "result",
                "session_id": msg.session_id,
                "duration_ms": msg.duration_ms,
                "duration_api_ms": msg.duration_api_ms,
                "total_cost_usd": msg.total_cost_usd,
                "is_error": msg.is_error,
                "num_turns": msg.num_turns,
                "usage": msg.usage,
                "model_usage": msg.model_usage,
            }
        elif isinstance(msg, SystemMessage):
            yield {"type": "system", "subtype": msg.subtype}
        elif isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        yield _process_tool_result(block)
        elif isinstance(msg, StreamEvent):
            event_data = msg.event
            if event_data.get("type") == "content_block_delta":
                delta = event_data.get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield {"type": "text_delta", "text": delta["text"]}
