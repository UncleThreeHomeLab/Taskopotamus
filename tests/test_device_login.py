import io
import subprocess
import unittest
from unittest.mock import Mock, patch

from src.agent.device_login import DeviceLogin, parse_instructions, start_device_login


class DeviceLoginTests(unittest.TestCase):
    def test_parser_handles_terminal_colors_and_only_uses_official_url(self):
        self.assertEqual(parse_instructions("https://auth.openai.com/codex/device"), (
            "https://auth.openai.com/codex/device", None,
        ))
        self.assertEqual(parse_instructions("\x1b[32mABCD-12345\x1b[0m"), (None, "ABCD-12345"))
        self.assertEqual(parse_instructions("https://example.invalid/login"), (None, None))

    def test_cli_output_stays_captured_and_success_is_reported(self):
        process = Mock()
        process.stdout = io.StringIO("https://auth.openai.com/codex/device\nABCD-12345\n")
        process.wait.return_value = 0
        with patch("src.agent.device_login.subprocess.Popen", return_value=process) as launch:
            login = start_device_login()
            self.assertTrue(login.wait())
        self.assertEqual(login.user_code, "ABCD-12345")
        self.assertEqual(launch.call_args.kwargs["stderr"], -2)
        self.assertEqual(launch.call_args.kwargs["stdout"], -1)
        self.assertEqual(launch.call_args.kwargs["env"]["TASKOPOTAMUS_WORKER_MODE"], "auth")

    def test_missing_instructions_returns_generic_error(self):
        process = Mock()
        process.stdout = io.StringIO("synthetic-private-error\n")
        process.wait.return_value = 77
        with patch("src.agent.device_login.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "Codex CLI could not start device login") as caught:
                start_device_login()
        self.assertNotIn("synthetic-private-error", str(caught.exception))

    def test_cancel_requests_eof_and_requires_remote_acknowledgment(self):
        process = Mock()
        process.stdout = io.StringIO("")
        process.stdin = io.StringIO()
        process.wait.return_value = 76
        with patch("src.agent.device_login.subprocess.Popen", return_value=process):
            login = DeviceLogin()
            login.finished.wait(timeout=2)
            login.cancel()
        self.assertTrue(process.stdin.closed)
        self.assertTrue(login.cleanup_confirmed)
        process.terminate.assert_not_called()

    def test_transport_loss_does_not_confirm_remote_cleanup(self):
        process = Mock()
        process.stdout = io.StringIO("")
        process.stdin = io.StringIO()
        process.wait.return_value = 255
        with patch("src.agent.device_login.subprocess.Popen", return_value=process):
            login = DeviceLogin()
            login.finished.wait(timeout=2)
            with self.assertRaisesRegex(RuntimeError, "could not be confirmed"):
                login.cancel()
            with self.assertRaisesRegex(RuntimeError, "could not be confirmed"):
                login.wait()
        self.assertFalse(login.cleanup_confirmed)

    def test_cancellation_timeout_remains_unconfirmed_after_local_termination(self):
        process = Mock(stdin=io.StringIO())
        process.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 5), -15]
        with patch("src.agent.device_login.subprocess.Popen", return_value=process), \
                patch("src.agent.device_login.threading.Thread"):
            login = DeviceLogin()
            with self.assertRaisesRegex(RuntimeError, "could not be confirmed"):
                login.cancel()
        process.terminate.assert_called_once()
        self.assertFalse(login.cleanup_confirmed)


if __name__ == "__main__":
    unittest.main()
