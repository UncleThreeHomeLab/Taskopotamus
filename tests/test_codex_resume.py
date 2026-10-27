import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from openai_codex import ApprovalMode, Codex, Sandbox
from openai_codex.client import CodexClient


class CodexResumeTests(unittest.TestCase):
    def test_metadata_only_resume_serializes_exclusion_and_allows_separate_history_read(self):
        history = [{"id": "synthetic-prior-turn", "items": []}]
        calls = []

        def request(method, payload, **_kwargs):
            calls.append((method, payload))
            if method == "thread/resume":
                return SimpleNamespace(thread=SimpleNamespace(id=payload["threadId"], turns=[]))
            if method == "thread/read":
                return SimpleNamespace(thread=SimpleNamespace(id=payload["threadId"], turns=history))
            self.fail(f"Unexpected SDK request: {method}")

        # Exercise the pinned SDK wrappers and wire serialization without starting a native client.
        client = object.__new__(CodexClient)
        client.request = Mock(side_effect=request)
        codex = object.__new__(Codex)
        codex._client = client
        thread = codex.thread_resume(
            "synthetic-existing-thread", include_turns=False, model="synthetic-model",
            approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.full_access,
            cwd="/workspace", developer_instructions="synthetic-current-instructions",
            config={"model_auto_compact_token_limit": 40000, "model_reasoning_effort": "medium"},
        )
        self.assertEqual(thread.id, "synthetic-existing-thread")
        self.assertEqual(calls[0][0], "thread/resume")
        payload = calls[0][1]
        self.assertIs(payload["excludeTurns"], True)
        self.assertEqual(payload["threadId"], thread.id)
        self.assertEqual(payload["model"], "synthetic-model")
        self.assertEqual(payload["approvalPolicy"], "never")
        self.assertEqual(payload["sandbox"], "danger-full-access")
        self.assertEqual(payload["developerInstructions"], "synthetic-current-instructions")
        self.assertEqual(payload["config"]["model_auto_compact_token_limit"], 40000)
        self.assertEqual(payload["config"]["model_reasoning_effort"], "medium")
        self.assertEqual(payload["cwd"], "/workspace")
        self.assertEqual(thread.read(include_turns=True).thread.turns, history)
        self.assertEqual(calls[1], ("thread/read", {"threadId": thread.id, "includeTurns": True}))
        self.assertEqual(len(calls), 2)
