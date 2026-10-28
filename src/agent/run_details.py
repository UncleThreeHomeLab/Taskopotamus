"""Project runtime evidence without exposing prompts, commands, output, or credentials."""
import re
import time
from queue import Empty
from uuid import uuid4

from openai_codex.generated.v2_all import SubAgentActivityThreadItem, Thread as NativeThread, ThreadReadResponse


EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
THREAD_STATES = ("notLoaded", "idle", "systemError", "active")
MAX_AGENTS = 20
MAX_ITEMS = 5000


def enum_value(value, allowed):
    value = getattr(value, "value", value)
    return value if isinstance(value, str) and value in allowed else "unavailable"


def duration(value):
    return value if type(value) is int and 0 <= value <= 86400000 else None


def model_name(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value) else None


def thread_metadata(thread):
    """Keep only native thread settings and private linkage needed for correlation."""
    if not isinstance(thread, NativeThread):
        return None
    return {
        "id": thread.id, "parent_id": thread.parent_thread_id,
        "model": model_name(thread.model), "reasoning": enum_value(thread.reasoning_effort, EFFORTS),
        "ephemeral": thread.ephemeral,
        "status": enum_value(getattr(thread.status.root, "type", None), THREAD_STATES),
    }


def read_thread_metadata(client, identifier, timeout):
    # ponytail: SDK 0.163 has no request timeout; retain this narrow bridge until its public API supports one.
    if timeout <= 0:
        raise TimeoutError("Native thread metadata timed out.")
    started = time.monotonic()
    request_id = str(uuid4())
    waiter = client._router.create_response_waiter(request_id)
    try:
        client._write_message({"id": request_id, "method": "thread/read",
                               "params": {"threadId": identifier, "includeTurns": False}})
        try:
            value = waiter.get(timeout=max(0, timeout - (time.monotonic() - started)))
        except Empty:
            raise TimeoutError("Native thread metadata timed out.") from None
        if isinstance(value, BaseException):
            raise value
        return thread_metadata(ThreadReadResponse.model_validate(value).thread)
    finally:
        # Late replies are read-only and must not accumulate orphan waiters or reach a later request.
        client._router.discard_response_waiter(request_id)


def cleanup_failure(stage, error, target=None):
    """Never retain RPC messages or thread identifiers in failure evidence."""
    kind = type(error).__name__
    code = getattr(error, "code", None)
    ephemeral = target.get("ephemeral") if isinstance(target, dict) else None
    message = getattr(error, "message", "")
    message = message.lower() if isinstance(message, str) else ""
    categories = [name for name, terms in (
        ("rollout", ("rollout",)), ("subagent", ("subagent", "sub-agent")),
        ("active", ("active", "running")), ("background", ("background",)),
        ("not_found", ("not found", "no such")), ("archive", ("archive",)),
        ("permission", ("permission",)), ("managed", ("managed",)),
    ) if any(term in message for term in terms)]
    return {
        "stage": enum_value(stage, ("list_loaded", "archive", "unarchive", "verify_unloaded", "pause", "runtime_exit")),
        "error_type": kind if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", kind) else "unavailable",
        "error_code": code if type(code) is int and -2147483648 <= code <= 2147483647 else None,
        "target_ephemeral": ephemeral if type(ephemeral) is bool else None,
        "target_role": enum_value(target.get("role", "unknown") if isinstance(target, dict) else "unknown",
                                  ("parent", "child", "unlinked", "unknown")),
        "categories": categories,
    }


def summarize_run(response, model, effort, subagents, *, parent_id=None, native_threads=None,
                  metadata_incomplete=False):
    details = {
        "configured_parent_model": model_name(model), "configured_parent_reasoning": effort,
        "configured_child_model": model_name(subagents.model), "configured_child_reasoning": subagents.effort,
        "actual_execution_model": None,
        "status": enum_value(response.status, ("completed", "failed", "interrupted", "inProgress")),
        "duration_ms": duration(getattr(response, "duration_ms", None)),
        "children": [], "commands": [], "verified_test_result": None,
        "agents": [], "observed_child_count": 0, "child_activity_count": 0,
        "agent_evidence_source": "parent_turn_items", "agent_evidence_truncated": False,
        "agent_evidence_incomplete": bool(metadata_incomplete),
        "runtime_parent_model": None, "runtime_parent_reasoning": "unavailable",
    }
    items = getattr(response, "items", None)
    if not isinstance(items, list):
        details["agent_evidence_incomplete"] = True
        items = []
    agents = {}
    for wrapped in items[:MAX_ITEMS]:
        item = getattr(wrapped, "root", wrapped)
        kind = getattr(item, "type", None)
        if isinstance(item, SubAgentActivityThreadItem):
            details["child_activity_count"] += 1
            identifier = item.agent_thread_id
            if identifier not in agents:
                if len(agents) == MAX_AGENTS:
                    details["agent_evidence_truncated"] = True
                    continue
                agents[identifier] = {
                    "child": len(agents) + 1, "activity": "unavailable",
                    "resolved_model": None, "resolved_reasoning": "unavailable",
                    "runtime_model": None, "runtime_reasoning": "unavailable",
                    "runtime_status": "unavailable", "ephemeral": None,
                }
            agent = agents[identifier]
            agent["activity"] = enum_value(item.kind, ("started", "interacted", "interrupted", "completed"))
            if enum_value(item.kind, ("started",)) == "started":
                # Native schema explicitly labels these settings resolved at child creation.
                agent["resolved_model"] = model_name(item.model)
                agent["resolved_reasoning"] = enum_value(item.reasoning_effort, EFFORTS)
        if kind == "commandExecution" and len(details["commands"]) < 20:
            code = getattr(item, "exit_code", None)
            details["commands"].append({
                "status": enum_value(item.status, ("inProgress", "completed", "failed", "declined")),
                "exit_code": code if type(code) is int and -65536 <= code <= 65536 else None,
                "duration_ms": duration(getattr(item, "duration_ms", None)),
            })
        if kind == "collabAgentToolCall" and len(details["children"]) < 20:
            states = getattr(item, "agents_states", {})
            details["children"].append({
                "operation": enum_value(item.tool, ("spawnAgent", "sendInput", "resumeAgent", "wait", "closeAgent", "sendMessage", "followupTask", "interruptAgent", "listAgents")),
                "status": enum_value(item.status, ("inProgress", "completed", "failed", "interrupted")),
                "requested_model": model_name(getattr(item, "model", None)),
                "agent_states": [enum_value(state.status, ("pendingInit", "running", "completed", "interrupted", "errored", "shutdown", "notFound"))
                                 for state in list(states.values())[:4]] if isinstance(states, dict) else [],
            })
        elif kind == "collabAgentToolCall":
            details["agent_evidence_truncated"] = True
    details["agent_evidence_truncated"] |= len(items) > MAX_ITEMS
    for index, (identifier, metadata) in enumerate((native_threads or {}).items()):
        if index > MAX_AGENTS:
            details["agent_evidence_truncated"] = True
            break
        if not isinstance(metadata, dict):
            details["agent_evidence_incomplete"] = True
            continue
        if identifier == parent_id:
            details["runtime_parent_model"] = model_name(metadata.get("model"))
            details["runtime_parent_reasoning"] = enum_value(metadata.get("reasoning"), EFFORTS)
        elif metadata.get("parent_id") == parent_id and parent_id is not None:
            # Same conversation lineage is not proof of activity in this request.
            if identifier not in agents:
                details["agent_evidence_incomplete"] = True
                continue
            agent = agents[identifier]
            agent.update(
                runtime_model=model_name(metadata.get("model")),
                runtime_reasoning=enum_value(metadata.get("reasoning"), EFFORTS),
                runtime_status=enum_value(metadata.get("status"), THREAD_STATES),
                ephemeral=metadata.get("ephemeral") if type(metadata.get("ephemeral")) is bool else None,
            )
        elif identifier in agents:
            details["agent_evidence_incomplete"] = True
            del agents[identifier]
    details["agents"] = list(agents.values())
    for ordinal, agent in enumerate(details["agents"], 1):
        agent["child"] = ordinal
    details["observed_child_count"] = len(agents)
    if details["children"] and not agents:
        details["agent_evidence_incomplete"] = True
    if any(not agent["resolved_model"] and not agent["runtime_model"] for agent in agents.values()):
        details["agent_evidence_incomplete"] = True
    return details
