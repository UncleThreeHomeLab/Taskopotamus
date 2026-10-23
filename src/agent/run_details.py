"""Project runtime evidence without exposing prompts, commands, output, or credentials."""
import re


def enum_value(value, allowed):
    value = getattr(value, "value", value)
    return value if isinstance(value, str) and value in allowed else "unavailable"


def duration(value):
    return value if type(value) is int and 0 <= value <= 86400000 else None


def model_name(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value) else None


def summarize_run(response, model, effort, subagents):
    details = {
        "configured_parent_model": model_name(model), "configured_parent_reasoning": effort,
        "configured_child_model": model_name(subagents.model), "configured_child_reasoning": subagents.effort,
        "actual_execution_model": None,
        "status": enum_value(response.status, ("completed", "failed", "interrupted", "inProgress")),
        "duration_ms": duration(getattr(response, "duration_ms", None)),
        "children": [], "commands": [], "verified_test_result": None,
    }
    items = getattr(response, "items", [])
    for wrapped in items if isinstance(items, list) else []:
        item = getattr(wrapped, "root", wrapped)
        kind = getattr(item, "type", None)
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
    return details
