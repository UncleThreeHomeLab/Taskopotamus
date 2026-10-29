"""Static Gmail MCP configuration; no Google credentials enter the worker."""


def google_config():
    config = {
        "mcp_servers.gmail.command": "/usr/local/bin/python",
        "mcp_servers.gmail.args": ["-I", "-m", "taskopotamus_worker.gmail_mcp"],
        "mcp_servers.gmail.env_vars": ["TASKOPOTAMUS_REQUEST_SCOPE", "TASKOPOTAMUS_ARTIFACT_AUTH", "TASKOPOTAMUS_AGENT_IP"],
        "mcp_servers.gmail.enabled_tools": ["search_gmail", "read_gmail"],
        "mcp_servers.gmail.required": True,
        "mcp_servers.gmail.tool_timeout_sec": 30,
        "mcp_servers.gmail.tools.search_gmail.approval_mode": "auto",
        "mcp_servers.gmail.tools.read_gmail.approval_mode": "auto",
    }
    tools = ["list_calendars", "list_calendar_events", "read_calendar_event", "create_calendar_event",
             "update_calendar_event", "delete_calendar_event", "calendar_operation_status", "list_calendar_event_colors"]
    config.update({
        "mcp_servers.calendar.command": "/usr/local/bin/python",
        "mcp_servers.calendar.args": ["-I", "-m", "taskopotamus_worker.calendar_mcp"],
        "mcp_servers.calendar.env_vars": ["TASKOPOTAMUS_REQUEST_SCOPE", "TASKOPOTAMUS_ARTIFACT_AUTH", "TASKOPOTAMUS_AGENT_IP"],
        "mcp_servers.calendar.enabled_tools": tools,
        "mcp_servers.calendar.required": True,
        "mcp_servers.calendar.tool_timeout_sec": 30,
        **{f"mcp_servers.calendar.tools.{name}.approval_mode": "auto" for name in tools},
    })
    return config
