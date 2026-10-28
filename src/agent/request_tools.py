"""One required worker process for the existing request-scoped tool catalog."""


TOOLS = (
    "send_message", "search_gmail", "read_gmail", "list_calendars", "list_calendar_events", "read_calendar_event",
    "create_calendar_event", "update_calendar_event", "delete_calendar_event", "calendar_operation_status",
    "get_agent_profile", "update_agent_profile",
)


def request_config():
    return {
        "mcp_servers.request_tools.command": "/usr/local/bin/python",
        "mcp_servers.request_tools.args": ["-I", "-m", "taskopotamus_worker.request_mcp"],
        "mcp_servers.request_tools.env_vars": ["TASKOPOTAMUS_REQUEST_SCOPE"],
        "mcp_servers.request_tools.enabled_tools": list(TOOLS),
        "mcp_servers.request_tools.required": True,
        "mcp_servers.request_tools.tool_timeout_sec": 120,
        **{f"mcp_servers.request_tools.tools.{name}.approval_mode": "auto" for name in TOOLS},
    }
