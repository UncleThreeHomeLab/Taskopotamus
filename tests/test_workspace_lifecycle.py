import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from codex_cli_bin import bundled_codex_path
from src.agent.device_login import start_device_login


if os.name != "nt":
    spec = importlib.util.spec_from_file_location("workspace_lifecycle", Path("deploy/worker/workspace_lifecycle.py"))
    lifecycle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lifecycle)


@unittest.skipIf(os.name == "nt", "Worker lifecycle requires Linux")
class WorkspacePolicyTests(unittest.TestCase):
    def test_runtime_alone_receives_validated_attachment_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = lifecycle.WorkspaceLifecycle(workspace=root / "workspace", auth_home=root / "auth" / ".codex")
            token = "s" * 43
            with patch.object(lifecycle, "directory"), patch.dict(os.environ, {"TASKOPOTAMUS_ARTIFACT_AUTH": token}):
                self.assertEqual(worker.child_command("runtime", ["--version"])[1]["TASKOPOTAMUS_ARTIFACT_AUTH"], token)
                self.assertNotIn("TASKOPOTAMUS_ARTIFACT_AUTH", worker.child_command("auth", ["--version"])[1])
            with patch.object(lifecycle, "directory"), patch.dict(os.environ, {"TASKOPOTAMUS_ARTIFACT_AUTH": "invalid"}):
                with self.assertRaises(ValueError):
                    worker.child_command("runtime", ["--version"])

    def test_production_defaults_and_invalid_settings(self):
        with patch.dict(os.environ, {}, clear=True):
            worker = lifecycle.WorkspaceLifecycle.configured()
        self.assertEqual((worker.retention, worker.sweep, worker.max_bytes), (604800, 3600, 1073741824))
        for value in ("", "0", "-1", "1.2", " 4", "١", str(2**64)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                lifecycle.positive_setting({"SETTING": value}, "SETTING", 1)

    def test_forced_command_rejects_modes_and_unapproved_commands(self):
        command = "/usr/local/bin/worker-codex "
        self.assertEqual(lifecycle.parse_remote(command + "runtime app-server --listen stdio://")[0], "runtime")
        self.assertEqual(lifecycle.parse_remote(command + 'auth -c \'cli_auth_credentials_store="file"\' login --device-auth')[0], "auth")
        self.assertEqual(lifecycle.parse_remote(command + 'runtime --config \'cli_auth_credentials_store="file"\' app-server --listen stdio://')[0], "runtime")
        self.assertEqual(lifecycle.parse_remote(command + "status"), ("status", []))
        for value in ("", "sh -c evil", command + "app-server", command + "root --version",
                      command + "runtime login --device-auth", command + "status extra", command + "auth exec evil",
                      command + "runtime app-server --listen tcp://0.0.0.0:9999"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                lifecycle.parse_remote(value)


@unittest.skipUnless(
    os.name != "nt" and getattr(os, "geteuid", lambda: -1)() == 0,
    "Run focused worker checks as root in an offline Linux test container",
)
class WorkspaceLifecycleTests(unittest.TestCase):
    def test_input_bytes_are_root_owned_read_only_and_capacity_refusal_survives_maintenance(self):
        import hashlib
        import io
        from src.worker.input_files import materialize_inputs, input_name

        content = b"synthetic-input"
        info = {"artifact_id": "a" * 32, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        materialize_inputs(
            {"request_tag": "b" * 64, "files": [info]},
            lambda _: io.BytesIO(content),
            self.worker.workspace,
            self.worker.max_bytes,
            runtime_gid=51001,
        )
        path = self.worker.workspace / ".incoming" / ("b" * 64) / input_name(info)
        self.assertEqual((path.stat().st_uid, path.stat().st_gid, path.stat().st_mode & 0o777), (0, 51001, 0o440))
        lifecycle.atomic_write(self.worker.state / "input-refusal.json", b'{"blocked":true}')
        self.worker.prepare()
        self.assertTrue(self.worker.status()["admission_blocked"])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.root.chmod(0o755)
        self.worker = lifecycle.WorkspaceLifecycle(
            workspace=self.root / "workspace", saved=self.root / "saved", state=self.root / "state",
            runtime_home=self.root / "runtime", auth_home=self.root / "auth",
            retention=5, sweep=1, runtime_uid=51001, auth_uid=51002,
        )
        self.worker.initialize()
        self.addCleanup(lifecycle.stop_uid, 51001)
        self.addCleanup(lifecycle.stop_uid, 51002)

    def credential(self, home, version):
        lifecycle.atomic_write(home / "auth.json", json.dumps({"synthetic_version": version}).encode(),
                               self.worker.auth_uid if home == self.worker.auth_home else self.worker.runtime_uid)

    def expire(self):
        value = self.worker.read_state()
        value["created_at"] = time.time() - 10
        self.worker.write_state(value)

    def test_fixed_epoch_survives_activity_and_cleanup_preserves_other_storage(self):
        created = self.worker.read_state()["created_at"]
        (self.worker.workspace / ".hidden").mkdir()
        (self.worker.workspace / ".hidden/file").write_text("synthetic work")
        target = self.worker.saved / "kept"
        target.write_text("synthetic saved")
        (self.worker.workspace / "outside-link").symlink_to(target)
        (self.worker.workspace / "outside-dir").symlink_to(self.worker.saved, target_is_directory=True)
        native = self.worker.runtime_home / "session"
        native.write_text("synthetic native")
        self.worker.prepare(now=created + 3)
        self.assertEqual(self.worker.read_state()["created_at"], created)
        self.worker.prepare(now=created + 5)
        self.assertEqual(list(self.worker.workspace.iterdir()), [])
        self.assertEqual(target.read_text(), "synthetic saved")
        self.assertEqual(native.read_text(), "synthetic native")
        self.assertEqual(self.worker.status()["generation"], 1)

    def test_partial_cleanup_retains_marker_and_retries_before_advancing_epoch(self):
        (self.worker.workspace / "folder").mkdir()
        self.expire()
        original = self.worker.read_state()
        with patch.object(lifecycle.shutil, "rmtree", side_effect=OSError("synthetic failure")) as deletion:
            deletion.avoids_symlink_attacks = True
            with self.assertRaises(OSError):
                self.worker.prepare()
        self.assertTrue(self.worker.read_state()["cleaning"])
        self.assertEqual(self.worker.read_state()["created_at"], original["created_at"])
        self.worker.prepare()
        self.assertFalse(self.worker.read_state()["cleaning"])
        self.assertEqual(self.worker.status()["generation"], 1)

    def test_lock_excludes_janitor_and_status_does_not_reenter_lock(self):
        self.expire()
        (self.worker.workspace / "active").write_text("synthetic")
        with self.worker.lock("workspace"):
            self.worker.maintenance()
            self.assertEqual(self.worker.status()["generation"], 0)
            self.assertTrue((self.worker.workspace / "active").exists())
        self.worker.maintenance()
        self.assertEqual(self.worker.status()["generation"], 1)

    def test_corrupt_state_and_unclassified_workspace_fail_closed(self):
        self.worker.epoch_file.write_text("invalid")
        with self.assertRaises(ValueError):
            self.worker.prepare()
        self.worker.epoch_file.unlink()
        sentinel = self.worker.workspace / "unclassified"
        sentinel.write_text("keep")
        with self.assertRaises(RuntimeError):
            self.worker.initialize()
        self.assertEqual(sentinel.read_text(), "keep")

    def test_admission_limit_and_scan_failure_do_not_delete_fresh_or_saved_files(self):
        sentinel = self.worker.workspace / "fresh"
        sentinel.write_bytes(b"x" * 100)
        saved = self.worker.saved / "saved"
        saved.write_text("keep")
        self.worker.max_bytes = 100
        with self.assertRaises(RuntimeError):
            self.worker.prepare(admission=True)
        with patch.object(self.worker, "usage", side_effect=OSError("synthetic")), self.assertRaises(OSError):
            self.worker.prepare(admission=True)
        self.assertTrue(sentinel.exists())
        self.assertEqual(saved.read_text(), "keep")

    def test_credential_recovery_preserves_refreshed_version_after_supervisor_crash(self):
        self.credential(self.worker.auth_home, "A")
        self.worker.begin_runtime_credentials()
        self.credential(self.worker.runtime_home, "B")
        orphan = subprocess.Popen(["sleep", "30"], user=self.worker.runtime_uid, group=self.worker.runtime_uid,
                                  start_new_session=True)
        self.addCleanup(orphan.wait)
        with patch.object(lifecycle, "CODEX", sys.executable):
            self.assertEqual(self.worker.run("auth", ["-c", "pass"]), 0)
        self.assertIsNotNone(orphan.poll())
        self.assertEqual(lifecycle.read_json(self.worker.auth_home / "auth.json")["synthetic_version"], "B")
        self.assertFalse(self.worker.pending.exists())
        self.worker.begin_runtime_credentials()
        self.assertEqual(lifecycle.read_json(self.worker.runtime_home / "auth.json")["synthetic_version"], "B")
        self.worker.recover_credentials()

    def test_startup_reloads_epoch_and_publishes_policy_for_clean_ssh_environment(self):
        before = self.worker.read_state()
        self.worker.initialize()
        self.assertEqual(self.worker.read_state(), before)
        self.assertEqual(lifecycle.read_json(self.worker.state / "policy.json"),
                         {"retention": 5, "sweep": 1, "max_bytes": 1073741824, "agent_ip": "10.203.17.30"})

    def test_credential_retry_after_destination_commit_keeps_newest_copy(self):
        self.credential(self.worker.auth_home, "A")
        self.worker.begin_runtime_credentials()
        self.credential(self.worker.runtime_home, "B")
        unlink = lifecycle.durable_unlink
        def interrupted(path):
            if path == self.worker.pending:
                raise OSError("synthetic crash")
            unlink(path)
        with patch.object(lifecycle, "durable_unlink", side_effect=interrupted), self.assertRaises(OSError):
            self.worker.recover_credentials()
        self.assertTrue(self.worker.pending.exists())
        self.worker.recover_credentials()
        self.assertEqual(lifecycle.read_json(self.worker.auth_home / "auth.json")["synthetic_version"], "B")

    def test_bad_credentials_keep_marker_and_original_private_copy(self):
        self.credential(self.worker.auth_home, "A")
        self.worker.begin_runtime_credentials()
        runtime = self.worker.runtime_home / "auth.json"
        for kind in ("symlink", "oversized", "fifo", "invalid"):
            runtime.unlink(missing_ok=True)
            if kind == "symlink":
                runtime.symlink_to(self.worker.auth_home / "auth.json")
            elif kind == "oversized":
                runtime.write_bytes(b"x" * (lifecycle.MAX_CREDENTIAL_BYTES + 1))
            elif kind == "fifo":
                os.mkfifo(runtime)
            else:
                runtime.write_text("not json")
            with self.subTest(kind=kind), self.assertRaises((ValueError, OSError)):
                self.worker.recover_credentials()
            self.assertTrue(self.worker.pending.exists())
            self.assertEqual(lifecycle.read_json(self.worker.auth_home / "auth.json")["synthetic_version"], "A")

    def test_missing_credentials_mean_signed_out_without_reimport(self):
        self.credential(self.worker.auth_home, "A")
        self.worker.begin_runtime_credentials()
        (self.worker.runtime_home / "auth.json").unlink()
        self.worker.recover_credentials()
        self.assertFalse((self.worker.auth_home / "auth.json").exists())
        self.worker.initialize()
        self.assertFalse((self.worker.auth_home / "auth.json").exists())

    def test_janitor_does_not_erase_pending_credential_recovery_or_auth_process(self):
        self.credential(self.worker.auth_home, "A")
        self.worker.begin_runtime_credentials()
        self.credential(self.worker.runtime_home, "B")
        auth = subprocess.Popen(["sleep", "30"], user=self.worker.auth_uid, group=self.worker.auth_uid)
        self.addCleanup(auth.wait)
        self.expire()
        with self.worker.lock("credentials"):
            self.worker.maintenance()
            self.assertIsNone(auth.poll())
        self.assertTrue(self.worker.pending.exists())
        self.worker.recover_credentials()
        lifecycle.stop_uid(self.worker.auth_uid)

    def test_runtime_has_no_privileges_and_background_descendants_are_stopped(self):
        self.credential(self.worker.auth_home, "A")
        program = ("import os,json,pathlib,subprocess; "
                   "p=subprocess.Popen(['sleep','30'],start_new_session=True); "
                   "v={'uid':os.getuid(),'pid':p.pid,'home':os.environ['HOME'],"
                   "'cap':next(x for x in pathlib.Path('/proc/self/status').read_text().splitlines() if x.startswith('CapEff:'))}; "
                   "pathlib.Path('observed.json').write_text(json.dumps(v))")
        with patch.object(lifecycle, "CODEX", sys.executable):
            self.assertEqual(self.worker.run("runtime", ["-c", program]), 0)
        value = json.loads((self.worker.workspace / "observed.json").read_text())
        self.assertEqual(value["uid"], self.worker.runtime_uid)
        self.assertEqual(int(value["cap"].split()[1], 16), 0)
        self.assertTrue(value["home"].startswith(str(self.worker.workspace)))
        self.assertEqual(lifecycle.uid_processes(self.worker.runtime_uid), [])
        self.assertFalse(self.worker.pending.exists())

    def test_runtime_cannot_access_auth_state_or_change_control_files(self):
        targets = [self.worker.state, self.worker.auth_home]
        program = "import os,sys;sys.exit(1 if any(os.access(p,os.R_OK|os.W_OK) for p in sys.argv[1:]) else 0)"
        result = subprocess.run([sys.executable, "-c", program, *map(str, targets)], user=self.worker.runtime_uid,
                                group=self.worker.runtime_uid, extra_groups=[])
        self.assertEqual(result.returncode, 0)
        command, environment, _ = self.worker.child_command("auth", ["--version"])
        self.assertIn(f"--reuid={self.worker.auth_uid}", command)
        self.assertEqual(environment["CODEX_HOME"], str(self.worker.auth_home))
        self.assertNotEqual(environment["CODEX_HOME"], str(self.worker.runtime_home))

    def test_pinned_app_server_starts_as_both_nonroot_identities_without_inference(self):
        with patch.object(lifecycle, "CODEX", str(bundled_codex_path())):
            for mode in ("auth", "runtime"):
                with self.subTest(mode=mode):
                    self.assertEqual(self.worker.run(mode, ["--config", 'cli_auth_credentials_store="file"',
                                                           "app-server", "--listen", "stdio://"]), 0)
        self.assertEqual(lifecycle.uid_processes(self.worker.runtime_uid), [])
        self.assertEqual(lifecycle.uid_processes(self.worker.auth_uid), [])

    def test_zombie_leader_with_live_writer_thread_is_stopped_before_cleanup(self):
        output = self.worker.workspace / "thread-writes"
        program = ("import ctypes,threading,time\n"
                   "def writer():\n"
                   " while True:\n"
                   f"  with open({str(output)!r},'a') as f: f.write('x')\n"
                   "  time.sleep(.02)\n"
                   "threading.Thread(target=writer).start()\n"
                   "ctypes.CDLL(None).pthread_exit(None)\n")
        child = subprocess.Popen([sys.executable, "-c", program], user=self.worker.runtime_uid,
                                 group=self.worker.runtime_uid, extra_groups=[])
        self.addCleanup(child.wait)
        until = time.monotonic() + 3
        observed = False
        while time.monotonic() < until:
            fields = dict(line.split(":", 1) for line in Path(f"/proc/{child.pid}/status").read_text().splitlines() if ":" in line)
            if fields["State"].lstrip().startswith("Z") and int(fields["Threads"]) > 1 and output.exists():
                observed = True
                break
            time.sleep(.02)
        self.assertTrue(observed, "Synthetic writer must reproduce a zombie leader with a live thread")
        self.assertIn(child.pid, lifecycle.uid_processes(self.worker.runtime_uid))
        lifecycle.stop_uid(self.worker.runtime_uid, grace=.3)
        child.wait(timeout=3)
        size = output.stat().st_size
        time.sleep(.1)
        self.assertEqual(output.stat().st_size, size)
        self.assertEqual(lifecycle.uid_processes(self.worker.runtime_uid), [])
        self.expire()
        self.worker.maintenance()
        self.assertFalse(output.exists())

    def test_startup_restricts_actual_posix_message_queue_creation(self):
        startup = Path("deploy/worker/start_worker.sh").read_text()
        command = next(line for line in startup.splitlines() if line.startswith("chmod 700 /run "))
        self.assertIn("/dev/mqueue", command)
        paths = [Path(value) for value in command.split()[2:]]
        modes = {path: path.stat().st_mode & 0o777 for path in paths}
        try:
            subprocess.run(["sh", "-c", command], check=True)
            program = ("import ctypes,errno,os,sys; c=ctypes.CDLL(None,use_errno=True); "
                       "c.mq_open.restype=ctypes.c_int; "
                       "name=b'/taskopotamus-synthetic-mqueue'; "
                       "fd=c.mq_open(name,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600,None); error=ctypes.get_errno(); "
                       "c.mq_close(fd) if fd>=0 else None; c.mq_unlink(name) if fd>=0 else None; "
                       "sys.exit(0 if fd==-1 and error==errno.EACCES else 1)")
            result = subprocess.run([sys.executable, "-c", program], user=self.worker.runtime_uid,
                                    group=self.worker.runtime_uid, extra_groups=[])
            self.assertEqual(result.returncode, 0)
        finally:
            for path, mode in modes.items():
                path.chmod(mode)

    def test_device_login_eof_acknowledges_only_after_delayed_remote_cleanup_and_lease_release(self):
        native = self.root / "synthetic-native"
        started = self.worker.auth_home / "started"
        cleaned = self.root / "cleanup-finished"
        native.write_text(f"#!{sys.executable}\nimport os,time,pathlib\n"
                          "pathlib.Path(os.environ['CODEX_HOME'],'started').write_text('started')\n"
                          "print('https://auth.openai.com/codex/device',flush=True)\n"
                          "print('TEST-12345',flush=True)\ntime.sleep(30)\n")
        native.chmod(0o755)
        transport = self.root / "synthetic-transport"
        arguments = {key: str(getattr(self.worker, key)) for key in ("workspace", "saved", "state", "runtime_home", "auth_home")}
        transport.write_text(f"#!{sys.executable}\nimport importlib.util,sys,time\nfrom pathlib import Path\n"
                             f"spec=importlib.util.spec_from_file_location('worker',{str(Path('deploy/worker/workspace_lifecycle.py').resolve())!r})\n"
                             "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n"
                             f"module.CODEX={str(native)!r}\noriginal=module.stop_uid\n"
                             "def delayed(uid):\n"
                             f" if uid==51002 and Path({str(started)!r}).exists(): time.sleep(.3)\n"
                             " original(uid)\n"
                             f" if uid==51002 and Path({str(started)!r}).exists(): Path({str(cleaned)!r}).write_text('finished')\n"
                             "module.stop_uid=delayed\n"
                             f"worker=module.WorkspaceLifecycle(**{arguments!r},runtime_uid=51001,auth_uid=51002)\n"
                             "sys.exit(worker.run('auth',sys.argv[1:]))\n")
        transport.chmod(0o755)
        with patch.dict(os.environ, {"CODEX_BIN": str(transport)}):
            login = start_device_login()
            self.addCleanup(login.cancel)
            self.assertTrue(started.exists())
            with self.assertRaises(BlockingIOError):
                with self.worker.lock("credentials"):
                    pass
            before = time.monotonic()
            login.cancel()
            self.assertGreaterEqual(time.monotonic() - before, .25)
            self.assertTrue(login.cleanup_confirmed)
            self.assertTrue(cleaned.exists())
            self.assertEqual(lifecycle.uid_processes(self.worker.auth_uid), [])
            with self.worker.lock("credentials"):
                pass


if __name__ == "__main__":
    unittest.main()
