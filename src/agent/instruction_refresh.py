from openai_codex.generated.v2_all import ThreadInjectItemsResponse


def refresh_instructions(thread, instructions):
    # ponytail: SDK 0.163 has no public injection wrapper; use its typed RPC until one is available.
    thread._client.request(
        "thread/inject_items",
        {
            "threadId": thread.id,
            "items": [{
                "type": "message", "role": "developer",
                "content": [{"type": "input_text", "text": (
                    "Current Taskopotamus application instructions follow. These replace earlier Taskopotamus "
                    "persona and capability instructions. Runtime security restrictions still apply.\n\n" + instructions
                )}],
            }],
        },
        response_model=ThreadInjectItemsResponse,
    )
