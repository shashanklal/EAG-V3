"""Action role — pure MCP dispatch with artifact handling.

No LLM calls. Executes exactly one tool call, handles artifact threshold,
and guards against art: handles in arguments.
"""
from __future__ import annotations

from mcp import ClientSession

from artifacts import ArtifactStore
from schemas import ActionResult, ToolCall

ARTIFACT_THRESHOLD_BYTES = 4096  # 4 KB


async def execute(
    session: ClientSession,
    tool_call: ToolCall,
    artifacts: ArtifactStore,
) -> ActionResult:
    """Execute a single MCP tool call. Returns ActionResult.

    If payload > 4KB, stores bytes as artifact and returns handle.
    Guards against art: handles in arguments.
    """
    # Guard: block art: handles in arguments
    for key, value in tool_call.arguments.items():
        if isinstance(value, str) and value.startswith("art:"):
            return ActionResult(
                descriptor=(
                    f"ERROR: argument '{key}' contains an artifact handle '{value}'. "
                    "Artifact handles are internal references, not file paths or URLs. "
                    "Use the actual path or URL instead."
                ),
                artifact_id=None,
                success=False,
            )

    # Dispatch MCP tool call
    try:
        result = await session.call_tool(tool_call.name, arguments=tool_call.arguments)
    except Exception as e:
        return ActionResult(
            descriptor=f"ERROR: tool '{tool_call.name}' failed: {str(e)[:200]}",
            artifact_id=None,
            success=False,
        )

    # Collapse content blocks to text
    text_parts = []
    for block in result.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
        elif hasattr(block, "data"):
            text_parts.append(str(block.data))
        else:
            text_parts.append(str(block))
    result_text = "\n".join(text_parts)

    # Handle empty results
    if not result_text.strip() or result_text.strip() in ("[]", "{}"):
        return ActionResult(
            descriptor=f"Tool '{tool_call.name}' returned empty results for arguments {tool_call.arguments}",
            artifact_id=None,
            success=False,
        )

    # Artifact threshold check
    payload_bytes = result_text.encode("utf-8")
    if len(payload_bytes) > ARTIFACT_THRESHOLD_BYTES:
        descriptor_preview = result_text[:200].replace("\n", " ")
        artifact = artifacts.put(
            data=payload_bytes,
            content_type="text/plain",
            source=f"tool:{tool_call.name}",
            descriptor=f"{tool_call.name} result ({len(payload_bytes)} bytes): {descriptor_preview[:80]}",
        )
        return ActionResult(
            descriptor=f"[artifact {artifact.id}, {artifact.size_bytes} bytes] preview: {descriptor_preview}",
            artifact_id=artifact.id,
            success=True,
        )

    return ActionResult(descriptor=result_text, artifact_id=None, success=True)
