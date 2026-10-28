"""Static Gmail MCP configuration; no Google credentials enter the worker."""


def google_config():
    return {
        "mcp_servers.gmail.command": "/usr/local/bin/python",
        "mcp_servers.gmail.args": ["-I", "-m", "taskopotamus_worker.gmail_mcp"],
        "mcp_servers.gmail.env_vars": ["TASKOPOTAMUS_ARTIFACT_AUTH", "TASKOPOTAMUS_AGENT_IP"],
        "mcp_servers.gmail.enabled_tools": ["search_gmail", "read_gmail"],
        "mcp_servers.gmail.required": True,
        "mcp_servers.gmail.tool_timeout_sec": 30,
        "mcp_servers.gmail.tools.search_gmail.approval_mode": "auto",
        "mcp_servers.gmail.tools.read_gmail.approval_mode": "auto",
    }
