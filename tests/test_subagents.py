import json
import importlib.util
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_cli_bin import bundled_codex_path
from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

from src.agent.subagents import SubagentPolicy
from src.agent.worker_keys import prepare_keys
from src.agent.worker_transport import command, configure_transport, main as transport_main
from src.agent.server import main as server_main


class SubagentTests(unittest.TestCase):
    def test_pinned_runtime_starts_full_access_thread_without_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            with Codex(CodexConfig(env={"CODEX_HOME": directory})) as codex:
                thread = codex.thread_start(
                    cwd=directory, sandbox=Sandbox.full_access, approval_mode=ApprovalMode.deny_all,
                    config=SubagentPolicy().config(),
                )
                self.assertTrue(thread.id)

    def test_server_refuses_full_access_with_a_local_runtime(self):
        with patch.dict(os.environ, {"CODEX_BIN": "/local/codex"}, clear=True), \
                patch("src.agent.server.os.umask"), patch("src.agent.server.Codex") as launch:
            with self.assertRaisesRegex(ValueError, "isolated worker"):
                server_main()
        launch.assert_not_called()

    def test_admin_cli_uses_ssh_and_cleans_its_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            key, public = Path(directory, "key"), Path(directory, "host.pub")
            key.write_text("synthetic-private-key")
            public.write_text("ssh-ed25519 synthetic-public-key test")
            with patch.dict(os.environ, {"WORKER_CLIENT_KEY_FILE": str(key), "WORKER_HOST_PUBLIC_KEY_FILE": str(public),
                                         "TASKOPOTAMUS_WORKER_MODE": "auth"}, clear=True), \
                    patch("sys.argv", ["codex-worker", "--version"]), \
                    patch("src.agent.worker_transport.subprocess.call", return_value=0) as launch:
                self.assertEqual(transport_main(), 0)
            arguments = launch.call_args.args[0]
            self.assertEqual(arguments[0], "ssh")
            self.assertEqual(shlex.split(arguments[-1]), ["/usr/local/bin/worker-codex", "auth", "--version"])
            self.assertFalse(Path(arguments[arguments.index("-i") + 1]).exists())

    def test_firewall_rejects_private_destinations_before_allowing_public_web(self):
        source = Path("deploy/worker/firewall.py")
        spec = importlib.util.spec_from_file_location("worker_firewall", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        commands = module.rules("10.203.17.18", "192.168.65.7")
        self.assertEqual(commands[0], ["iptables", "-P", "OUTPUT", "DROP"])
        allow_web = next(index for index, rule in enumerate(commands) if "80,443" in rule)
        for subnet in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
            self.assertLess(next(index for index, rule in enumerate(commands) if subnet in rule), allow_web)
        self.assertEqual(commands[-1], ["ip6tables", "-P", "OUTPUT", "DROP"])
        dns_rules = [rule for rule in commands if "192.168.65.7" in rule]
        self.assertEqual({rule[rule.index("-p") + 1] for rule in dns_rules}, {"udp", "tcp"})
        self.assertTrue(all(rule[rule.index("--dport") + 1] == "53" for rule in dns_rules))
        self.assertTrue(all(commands.index(rule) < allow_web for rule in dns_rules))
        for address in ("127.0.0.1", "169.254.169.254", "0.0.0.0", "8.8.8.8", "bad"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                module.rules(address, "1.1.1.1")
        for resolver in ("0.0.0.0", "127.0.0.1", "169.254.169.254", "224.0.0.1", "bad"):
            with self.subTest(resolver=resolver), self.assertRaises(ValueError):
                module.rules("10.203.17.18", resolver)

    def test_child_defaults_are_explicit_and_limits_reject_invalid_settings(self):
        config = SubagentPolicy().config()
        self.assertEqual(config["agents.default_subagent_model"], "gpt-6-luna")
        self.assertEqual(config["agents.default_subagent_reasoning_effort"], "high")
        self.assertEqual(config["agents.max_concurrent_threads_per_session"], 2)
        self.assertEqual(config["agents.max_depth"], 1)
        for settings in ({"model": ""}, {"model": "bad\nmodel"}, {"effort": "invalid"},
                         {"max_threads": 0}, {"max_threads": 5}, {"max_threads": True}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                SubagentPolicy(**settings)

    def test_pinned_native_runtime_accepts_config_without_starting_a_model_turn(self):
        from src.agent.attachment_tools import attachment_config
        from src.agent.google_tools import google_config
        with tempfile.TemporaryDirectory() as directory:
            config = {**SubagentPolicy().config(), **attachment_config(), **google_config()}
            Path(directory, "config.toml").write_text("\n".join(f"{key} = {json.dumps(value)}" for key, value in config.items()))
            result = subprocess.run([str(bundled_codex_path()), "app-server", "--strict-config"],
                                    input="", capture_output=True, text=True, timeout=20,
                                    env={**os.environ, "CODEX_HOME": directory})
        self.assertEqual(result.returncode, 0, "Pinned Codex runtime rejected subagent configuration.")

    def test_ssh_transport_pins_host_and_quotes_arguments_without_sending_controller_secrets(self):
        arguments = ["app-server", "-c", 'model="synthetic"; touch /outside; #']
        result = command(arguments, {"TASKOPOTAMUS_SSH_KEY": "/tmp/key", "TASKOPOTAMUS_SSH_HOSTS": "/tmp/hosts",
                                     "TASKOPOTAMUS_WORKER_MODE": "runtime"})
        self.assertEqual(shlex.split(result[-1]), ["/usr/local/bin/worker-codex", "runtime", *arguments])
        self.assertIn("StrictHostKeyChecking=yes", result)
        self.assertIn("ClearAllForwardings=yes", result)
        self.assertIn("ForwardAgent=no", result)
        self.assertEqual([value for value in result if value.startswith("SendEnv=")],
                         ["SendEnv=TASKOPOTAMUS_PROFILE_AUTH", "SendEnv=TASKOPOTAMUS_ARTIFACT_AUTH"])
        for mode in (None, "root", "runtime; evil"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                command([], {"TASKOPOTAMUS_WORKER_MODE": mode})

    def test_private_transport_key_is_copied_to_private_temporary_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            key, public = Path(directory, "key"), Path(directory, "host.pub")
            key.write_text("synthetic-private-key")
            public.write_text("ssh-ed25519 synthetic-public-key test")
            with patch.dict(os.environ, {"WORKER_CLIENT_KEY_FILE": str(key), "WORKER_HOST_PUBLIC_KEY_FILE": str(public)}):
                with configure_transport() as root:
                    self.assertEqual(Path(root, "client_key").read_text(), "synthetic-private-key")
                    self.assertEqual(Path(root, "client_key").stat().st_mode & 0o777, 0o600)
                    self.assertTrue(Path(root, "known_hosts").read_text().startswith("[worker]:2222 ssh-ed25519 "))
                self.assertFalse(Path(root).exists())

    def test_worker_key_generation_preserves_existing_keys_and_rejects_partial_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare_keys(directory)
            before = {path.name: path.read_bytes() for path in Path(directory).iterdir()}
            prepare_keys(directory)
            self.assertEqual({path.name: path.read_bytes() for path in Path(directory).iterdir()}, before)
            Path(directory, "worker_client_key.pub").unlink()
            with self.assertRaises(ValueError):
                prepare_keys(directory)
