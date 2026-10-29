"""One required worker process for the existing request-scoped tool catalog."""
import re
from urllib.parse import urlsplit


TOOLS = (
    "send_message", "search_gmail", "read_gmail", "list_calendars", "list_calendar_events", "read_calendar_event",
    "create_calendar_event", "update_calendar_event", "delete_calendar_event", "calendar_operation_status", "list_calendar_event_colors",
    "get_agent_profile", "update_agent_profile",
)


def request_config(connection=None):
    if connection is not None:
        if not isinstance(connection, dict) or set(connection) != {"url", "token"}:
            raise ValueError("Invalid private tool transport.")
        url, token = connection["url"], connection["token"]
        parts = urlsplit(url) if isinstance(url, str) else None
        if (parts is None or parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port
                or parts.path != "/mcp" or parts.username or parts.password or parts.query or parts.fragment
                or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)):
            raise ValueError("Invalid private tool transport.")
        return {"mcp_servers.request_tools": {
            "url": url, "http_headers": {"Authorization": "Bearer " + token},
            "enabled_tools": list(TOOLS), "required": True, "tool_timeout_sec": 120,
            "tools": {name: {"approval_mode": "auto"} for name in TOOLS},
        }}
    return {
        "mcp_servers.request_tools.command": "/usr/local/bin/python",
        "mcp_servers.request_tools.args": ["-I", "-m", "taskopotamus_worker.request_mcp"],
        "mcp_servers.request_tools.env_vars": ["TASKOPOTAMUS_REQUEST_SCOPE"],
        "mcp_servers.request_tools.enabled_tools": list(TOOLS),
        "mcp_servers.request_tools.required": True,
        "mcp_servers.request_tools.tool_timeout_sec": 120,
        **{f"mcp_servers.request_tools.tools.{name}.approval_mode": "auto" for name in TOOLS},
    }
