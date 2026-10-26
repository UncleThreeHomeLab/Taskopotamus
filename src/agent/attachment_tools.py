"""Static worker tool configuration; scoped credentials stay in process environment."""


def attachment_config():
    return {
        "mcp_servers.reply_attachments.command": "/usr/local/bin/python",
        "mcp_servers.reply_attachments.args": ["-I", "-m", "taskopotamus_worker.message_mcp"],
        "mcp_servers.reply_attachments.env_vars": ["TASKOPOTAMUS_ARTIFACT_AUTH", "TASKOPOTAMUS_AGENT_IP"],
        "mcp_servers.reply_attachments.enabled_tools": ["send_message"],
        "mcp_servers.reply_attachments.required": True,
        "mcp_servers.reply_attachments.tool_timeout_sec": 120,
        "mcp_servers.reply_attachments.tools.send_message.approval_mode": "auto",
    }
