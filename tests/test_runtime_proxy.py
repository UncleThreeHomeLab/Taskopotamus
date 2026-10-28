"""Real root/UID/process checks, restricted to isolated offline Linux containers."""
import importlib.util
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from src.worker.request_scope import load_scope


NATIVE = r'''
import json,os,pathlib,subprocess,sys
for line in sys.stdin:
    value=json.loads(line); method=value.get('method')
    if 'id' not in value: continue
    result={}
    if method=='spawn':
        program="import pathlib,time\np=pathlib.Path('descendant-observed')\nwhile True:\n p.open('a').write(pathlib.Path("+repr(os.environ['TASKOPOTAMUS_REQUEST_SCOPE'])+").read_text())\n time.sleep(.01)\n"
        child=subprocess.Popen([sys.executable,'-u','-c',program],start_new_session=True)
        result={'pid':child.pid}
    elif method=='flood':
        while True: print('x'*65536,flush=True)
    elif method=='forge':
        print(json.dumps({'id':str(value['id']), 'method':'taskopotamus/runtime/close','params':{}}),flush=True)
    elif method=='refresh':
        pathlib.Path(os.environ['CODEX_HOME'],'auth.json').write_text('{"synthetic_version":"refreshed"}')
    print(json.dumps({'id':value['id'],'result':result}),flush=True)
'''

HARNESS = r'''
import importlib.util,json,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('lifecycle',Path('deploy/worker/workspace_lifecycle.py'))
lifecycle=importlib.util.module_from_spec(spec);spec.loader.exec_module(lifecycle)
root=Path(sys.argv[1]); lifecycle.CODEX=sys.executable
worker=lifecycle.WorkspaceLifecycle(workspace=root/'workspace',saved=root/'saved',state=root/'state',
 runtime_home=root/'native',auth_home=root/'auth',retention=30,runtime_uid=51003,auth_uid=51004)
worker.initialize()
if sys.argv[2]!='pinned-native':
 lifecycle.atomic_write(worker.auth_home/'auth.json',b'{"synthetic_version":"initial"}',uid=51004)
def stage(_token):
 pids=lifecycle.uid_processes(51003)
 assert len(pids)==1
 assert '\nState:\tT' in Path('/proc',str(pids[0]),'status').read_text()
 (root/'stage-paused').write_text('true')
 if (root/'stall-stage').exists(): time.sleep(30)
 if (root/'capacity-refusal').exists():
  lifecycle.atomic_write(worker.state/'admission.json',b'{"usage_bytes":0,"admission_blocked":true}')
  raise RuntimeError('Synthetic capacity refusal')
worker.stage_inputs=stage
try:
 if sys.argv[2]=='pinned-native':
  from codex_cli_bin import bundled_codex_path
  lifecycle.CODEX=str(bundled_codex_path())
  arguments=['--config','cli_auth_credentials_store="file"','app-server','--listen','stdio://']
 else: arguments=['-u','-c',sys.argv[2]]
 worker.run('warm',arguments)
except Exception:
 sys.exit(1)
'''


@unittest.skipUnless(os.name != "nt" and getattr(os, "geteuid", lambda: -1)() == 0,
                     "Run privileged process tests as root in an offline test container")
class RuntimeProxyTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("lifecycle", Path("deploy/worker/workspace_lifecycle.py"))
        self.lifecycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.lifecycle)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.root.chmod(0o755)
        self.process = subprocess.Popen([sys.executable, "-u", "-c", HARNESS, str(self.root), NATIVE],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup)
        self.buffer = bytearray()
        self.rpc("initialize", {})

    def cleanup(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for uid in (51003, 51004):
            self.lifecycle.stop_uid(uid)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()

    def send(self, method, params):
        identifier = str(uuid.uuid4())
        self.process.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}).encode() + b"\n")
        self.process.stdin.flush()
        return identifier

    def receive(self, identifier, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                value = json.loads(line)
                if value.get("id") == identifier and "result" in value:
                    return value["result"]
                continue
            if select.select([self.process.stdout], [], [], .1)[0]:
                content = os.read(self.process.stdout.fileno(), 65536)
                if not content:
                    raise EOFError("Supervisor closed after cleanup.")
                self.buffer.extend(content)
        raise TimeoutError("Synthetic runtime response unavailable.")

    def rpc(self, method, params):
        return self.receive(self.send(method, params))

    def begin(self, token="a", lifetime=10):
        return self.rpc("taskopotamus/runtime/begin", {"idle_seconds": 10, "scope": {
            "profile_token": "synthetic-profile", "artifact_token": token * 43,
            "profile_url": "http://mcp:8080", "agent_ip": "10.203.17.30", "deadline": time.time() + lifetime}})

    def test_old_descendants_die_before_new_scope_and_close_ack_follows_recovery(self):
        self.begin()
        retained = self.lifecycle.uid_processes(51003)
        self.assertEqual(len(retained), 1)
        self.assertEqual((self.root / "stage-paused").read_text(), "true")
        descendant = self.rpc("spawn", {})["pid"]
        until = time.monotonic() + 2
        observed = self.root / "workspace" / "descendant-observed"
        while not observed.exists() and time.monotonic() < until:
            time.sleep(.01)
        self.assertTrue(observed.exists())
        self.rpc("taskopotamus/runtime/end", {"unloaded": True})
        self.assertEqual(self.lifecycle.uid_processes(51003), retained)
        self.assertNotIn(descendant, self.lifecycle.uid_processes(51003))
        self.assertFalse((self.root / "runtime-scope" / "current.json").exists())
        size = observed.stat().st_size
        self.begin("b")
        time.sleep(.05)
        self.assertEqual(observed.stat().st_size, size)
        self.assertEqual(self.lifecycle.uid_processes(51003), retained)
        fresh = load_scope(self.root / "runtime-scope" / "current.json", runtime_gid=51003)
        self.assertEqual(fresh["artifact_token"], "b" * 43)
        self.rpc("refresh", {})
        self.assertTrue(self.rpc("taskopotamus/runtime/close", {})["closed"])
        self.process.wait(timeout=3)
        self.assertEqual(self.lifecycle.uid_processes(51003), [])
        self.assertFalse((self.root / "state" / "runtime-credentials-pending.json").exists())
        self.assertEqual(json.loads((self.root / "auth" / "auth.json").read_text())["synthetic_version"], "refreshed")

    def test_native_output_cannot_invoke_privileged_controls(self):
        self.begin()
        self.rpc("forge", {})
        self.assertTrue((self.root / "runtime-scope" / "current.json").exists())
        self.assertIsNone(self.process.poll())

    def test_stalled_privileged_input_staging_obeys_request_deadline(self):
        (self.root / "stall-stage").touch()
        started = time.monotonic()
        with self.assertRaises(EOFError):
            self.begin(lifetime=.3)
        self.process.wait(timeout=3)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.lifecycle.uid_processes(51003), [])
        self.assertFalse((self.root / "state" / "runtime-credentials-pending.json").exists())

    def test_stalled_controller_output_cannot_prevent_cleanup(self):
        self.begin(lifetime=.5)
        self.send("flood", {})
        self.process.wait(timeout=4)
        self.assertEqual(self.lifecycle.uid_processes(51003), [])
        self.assertFalse((self.root / "runtime-scope" / "current.json").exists())

    def test_idle_workspace_expiry_stops_native_and_cleans_workspace(self):
        self.begin()
        epoch = self.root / "state" / "workspace.json"
        state = json.loads(epoch.read_text())
        state["created_at"] = time.time() - 29.7
        epoch.write_text(json.dumps(state))
        self.rpc("taskopotamus/runtime/end", {"unloaded": True})
        self.process.wait(timeout=4)
        self.assertEqual(self.lifecycle.uid_processes(51003), [])
        self.assertEqual(list((self.root / "workspace").iterdir()), [])
        self.assertEqual(json.loads(epoch.read_text())["generation"], 1)

    def test_real_pinned_native_and_sdk_reuse_proxy_with_new_mcp_snapshot(self):
        from openai_codex import Codex, CodexConfig, Sandbox, ApprovalMode
        from src.agent.runtime_session import RuntimeSessions
        from src.agent.worker_transport import WorkspaceStorageError

        self.cleanup()
        # A second isolated root supervises the actual pinned runtime; no inference is requested.
        root = self.root / "real-native"
        root.mkdir(mode=0o755)
        factory = lambda: Codex(CodexConfig(launch_args_override=[sys.executable, "-u", "-c", HARNESS,
                                                                 str(root), "pinned-native"]))
        def status():
            return {**json.loads((root / "state" / "admission.json").read_text()),
                    "runtime_active": (root / "state" / "runtime-credentials-pending.json").exists()}

        sessions = RuntimeSessions(factory, cleanup_status=status)
        transport = None
        try:
            observed = root / "workspace" / "snapshots"
            program = ("import os; from pathlib import Path; from mcp.server import MCPServer; "
                       "from taskopotamus_worker.request_scope import load_scope; "
                       "snapshot=load_scope(os.environ['TASKOPOTAMUS_REQUEST_SCOPE'],runtime_gid=51003); "
                       f"Path({str(observed)!r}).open('a').write(str(os.getpid())+':'+snapshot['artifact_token']+'\\n'); "
                       "MCPServer('synthetic').run(transport='stdio')")
            program = ("import traceback; from pathlib import Path\ntry:\n " + program + "\nexcept BaseException:\n "
                       + f"Path({str(root / 'workspace' / 'mcp-error')!r}).write_text(traceback.format_exc()); raise\n")
            config = {"features.apps": False, "web_search": "disabled", "mcp_servers.synthetic.command": sys.executable,
                      "mcp_servers.synthetic.args": ["-c", program], "mcp_servers.synthetic.required": True,
                      "mcp_servers.synthetic.env_vars": ["TASKOPOTAMUS_REQUEST_SCOPE"]}
            with sessions.lease("42", {}, "synthetic-profile", "a" * 43, 20) as first:
                transport = first.client._client._proc
                try:
                    thread = first.client.thread_start(cwd=str(root / "workspace"), config=config,
                                                       sandbox=Sandbox.full_access, approval_mode=ApprovalMode.deny_all)
                except Exception:
                    error_file = root / "workspace" / "mcp-error"
                    self.fail(error_file.read_text() if error_file.exists() else "Synthetic MCP failed before its startup check")
            retained = self.lifecycle.uid_processes(51003)
            with sessions.lease("42", {}, "synthetic-profile", "b" * 43, 20) as second:
                self.assertTrue(second.reused)
                second.resume(thread.id, cwd=str(root / "workspace"), config=config,
                              sandbox=Sandbox.full_access, approval_mode=ApprovalMode.deny_all)
            self.assertEqual(self.lifecycle.uid_processes(51003), retained)
            records = [item.split(":") for item in observed.read_text().splitlines()]
            self.assertEqual([item[1] for item in records], ["a" * 43, "b" * 43])
            self.assertNotEqual(records[0][0], records[1][0])
            (root / "capacity-refusal").touch()
            with self.assertRaises(WorkspaceStorageError), sessions.lease("42", {}, "synthetic-profile", "c" * 43, 20):
                self.fail("Refused input staging must not enter an active request.")
            self.assertFalse(sessions.failed)
            self.assertFalse(status()["runtime_active"])
        finally:
            sessions.shutdown()
        self.assertEqual(self.lifecycle.uid_processes(51003), [])
        self.assertIsNotNone(transport.poll())
        for stream in (transport.stdin, transport.stdout, transport.stderr):
            self.assertTrue(stream.closed)
