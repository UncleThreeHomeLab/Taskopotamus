import io
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

    def test_missing_instructions_returns_generic_error(self):
        process = Mock()
        process.stdout = io.StringIO("synthetic-private-error\n")
        process.wait.return_value = 1
        with patch("src.agent.device_login.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "Codex CLI could not start device login") as caught:
                start_device_login()
        self.assertNotIn("synthetic-private-error", str(caught.exception))

    def test_cancel_stops_running_login(self):
        process = Mock()
        process.stdout = io.StringIO("")
        process.poll.return_value = None
        with patch("src.agent.device_login.subprocess.Popen", return_value=process):
            login = DeviceLogin()
            login.finished.wait(timeout=2)
            login.cancel()
        process.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
