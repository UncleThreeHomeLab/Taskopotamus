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
    elif method=='identity': result={'pid':os.getpid()}
    elif method=='thread/resume':
        thread={'id':value['params']['threadId'],'sessionId':'synthetic-session','cliVersion':'synthetic',
                'createdAt':0,'updatedAt':0,'cwd':os.getcwd(),'modelProvider':'synthetic','ephemeral':False,
                'preview':'','source':'cli','status':{'type':'idle'},'turns':[]}
        if not value['params'].get('excludeTurns',False):
            thread['turns']=[{'id':'synthetic-turn','status':'completed','items':[
                {'id':'synthetic-message','type':'agentMessage','text':'x'*(4*1024*1024+1)}]}]
        result={'thread':thread,'model':'synthetic','modelProvider':'synthetic','cwd':os.getcwd(),
                'approvalPolicy':'never','approvalsReviewer':'user','sandbox':{'type':'dangerFullAccess'}}
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
real_monotonic=time.monotonic
def shifted_monotonic():
 try: offset=float((root/'clock-offset').read_text())
 except FileNotFoundError: offset=0
 return real_monotonic()+offset
time.monotonic=shifted_monotonic
worker=lifecycle.WorkspaceLifecycle(workspace=root/'workspace',saved=root/'saved',state=root/'state',
 runtime_home=root/'native',auth_home=root/'auth',retention=30,runtime_uid=51003,auth_uid=51004)
worker.initialize()
if sys.argv[2]!='pinned-native':
 lifecycle.atomic_write(worker.auth_home/'auth.json',b'{"synthetic_version":"initial"}',uid=51004)
staging_count=0
def stage(_token):
 global staging_count
 if (root/'long-retention').exists(): worker.retention=3600
 pids=lifecycle.uid_processes(51003)
 assert len(pids)==(2 if staging_count else 1)
 staging_count+=1
 assert all('\nState:\tT' in Path('/proc',str(pid),'status').read_text() for pid in pids)
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
        result = self.rpc("taskopotamus/runtime/begin", {"scope": {
            "profile_token": "synthetic-profile", "artifact_token": token * 43,
            "profile_url": "http://mcp:8080", "agent_ip": "10.203.17.30", "deadline": time.time() + lifetime}})
        self.assertEqual(set(result["phase_timings_ms"]), {
            "boundary", "prepare", "stage_inputs", "write_scope", "helper_transition"})
        self.assertTrue(all(type(value) is int and value >= 0 for value in result["phase_timings_ms"].values()))
        return result

    def test_old_descendants_die_before_new_scope_and_close_ack_follows_recovery(self):
        self.begin()
        retained = self.lifecycle.uid_processes(51003)
        self.assertEqual(len(retained), 2)
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

    def test_helper_private_control_is_inaccessible_to_same_uid_and_both_pause(self):
        self.begin()
        native = self.rpc("identity", {})["pid"]
        retained = self.lifecycle.uid_processes(51003)
        helper, = set(retained) - {native}
        program = """import json,os,sys
results=[]
for suffix,flags in [('environ',os.O_RDONLY),('fd/0',os.O_RDONLY),('fd/0',os.O_WRONLY),('fd/1',os.O_RDONLY)]:
 try:
  descriptor=os.open('/proc/'+sys.argv[1]+'/'+suffix,flags)
 except PermissionError: results.append('denied')
 else:
  os.close(descriptor);results.append('opened')
print(json.dumps(results))
"""
        probe = subprocess.run(["setpriv", "--reuid=51003", "--regid=51003", "--clear-groups",
                                sys.executable, "-I", "-c", program, str(helper)],
                               capture_output=True, text=True, check=True, timeout=3)
        self.assertEqual(json.loads(probe.stdout), ["denied"] * 4)
        descendant = self.rpc("spawn", {})["pid"]
        self.rpc("taskopotamus/runtime/end", {"unloaded": True})
        self.assertNotIn(descendant, self.lifecycle.uid_processes(51003))
        self.assertEqual(set(self.lifecycle.uid_processes(51003)), set(retained))
        for pid in retained:
            self.assertIn("\nState:\tT", Path("/proc", str(pid), "status").read_text())
        self.assertFalse((self.root / "runtime-scope" / "current.json").exists())
        self.begin("b")
        self.assertEqual(set(self.lifecycle.uid_processes(51003)), set(retained))

    def test_helper_death_discards_scope_and_all_uid_processes(self):
        import signal
        self.begin()
        native = self.rpc("identity", {})["pid"]
        helper, = set(self.lifecycle.uid_processes(51003)) - {native}
        os.kill(helper, signal.SIGKILL)
        self.process.wait(timeout=4)
        self.assertEqual(self.lifecycle.uid_processes(51003), [])
        self.assertFalse((self.root / "runtime-scope" / "current.json").exists())
        self.assertFalse((self.root / "state" / "runtime-credentials-pending.json").exists())

    def test_native_output_cannot_invoke_privileged_controls(self):
        self.begin()
        self.rpc("forge", {})
        self.assertTrue((self.root / "runtime-scope" / "current.json").exists())
        self.assertIsNone(self.process.poll())

    def test_idle_runtime_survives_former_cutoff_without_request_authority(self):
        (self.root / "long-retention").touch()
        self.begin()
        retained = self.lifecycle.uid_processes(51003)
        self.assertEqual(len(retained), 2)
        descendant = self.rpc("spawn", {})["pid"]
        self.rpc("taskopotamus/runtime/end", {"unloaded": True})
        self.assertNotIn(descendant, self.lifecycle.uid_processes(51003))
        self.lifecycle.atomic_write(self.root / "clock-offset", b"301")
        time.sleep(.3)
        self.assertIsNone(self.process.poll())
        self.assertEqual(self.lifecycle.uid_processes(51003), retained)
        self.assertIn("\nState:\tT", Path("/proc", str(retained[0]), "status").read_text())
        self.assertFalse((self.root / "runtime-scope" / "current.json").exists())
        self.begin("b")
        self.assertEqual(self.lifecycle.uid_processes(51003), retained)
        fresh = load_scope(self.root / "runtime-scope" / "current.json", runtime_gid=51003)
        self.assertEqual(fresh["artifact_token"], "b" * 43)

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
        self.begin()
        self.rpc("taskopotamus/runtime/end", {"unloaded": True})
        self.begin(lifetime=.5)
        self.send("flood", {})
        self.process.wait(timeout=4)
        self.assertEqual(self.lifecycle.uid_processes(51003), [])
        self.assertFalse((self.root / "runtime-scope" / "current.json").exists())

    def test_metadata_resume_succeeds_while_single_history_response_exceeds_transport_limit(self):
        from openai_codex.generated.v2_all import ThreadResumeResponse

        self.begin()
        identifier = "synthetic-existing-thread"
        response = self.rpc("thread/resume", {"threadId": identifier, "excludeTurns": True})
        resumed = ThreadResumeResponse.model_validate(response)
        self.assertEqual(resumed.thread.id, identifier)
        self.assertEqual(resumed.thread.turns, [])
        self.assertIsNone(self.process.poll())
        with self.assertRaises(EOFError):
            self.rpc("thread/resume", {"threadId": identifier})
        self.process.wait(timeout=3)
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
                first.parent_thread_id = thread.id
            retained = self.lifecycle.uid_processes(51003)
            with sessions.lease("42", {}, "synthetic-profile", "b" * 43, 20) as second:
                self.assertTrue(second.reused)
                second.resume(thread.id, cwd=str(root / "workspace"), config=config,
                              sandbox=Sandbox.full_access, approval_mode=ApprovalMode.deny_all)
                second.parent_thread_id = thread.id
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

    def test_actual_http_helper_catalog_and_loaded_thread_remain_initialized(self):
        from openai_codex import Codex, CodexConfig, Sandbox, ApprovalMode
        from src.agent.runtime_session import RuntimeSessions
        from src.agent.request_tools import request_config, TOOLS
        from src.agent.instruction_refresh import refresh_instructions
        from openai_codex.generated.v2_all import ThreadReadResponse

        self.cleanup()
        root = self.root / "warm-native"
        root.mkdir(mode=0o755)
        (root / "long-retention").touch()
        sessions = RuntimeSessions(lambda: Codex(CodexConfig(launch_args_override=[
            sys.executable, "-u", "-c", HARNESS, str(root), "pinned-native"])))
        try:
            counts = []
            for index, token in enumerate(("a", "b", "c")):
                with sessions.lease("42", {}, "synthetic-profile", token * 43, 20) as lease:
                    options = {"cwd": str(root / "workspace"), "sandbox": Sandbox.full_access,
                               "approval_mode": ApprovalMode.deny_all,
                               "config": {**request_config(lease.tools), "features.apps": False,
                                          "web_search": "disabled"}}
                    if index == 0:
                        thread = lease.client.thread_start(**options)
                        refresh_instructions(thread, "Synthetic persisted history marker")
                    else:
                        acquired = lease.resume(thread.id, **options)
                        self.assertIs(acquired, thread)
                        self.assertEqual(lease.thread_action, "reuse")
                        response = lease.client._client.request("thread/read", {
                            "threadId": thread.id, "includeTurns": True}, response_model=ThreadReadResponse)
                        self.assertEqual(response.thread.id, thread.id)
                    lease.bind(thread, options, "synthetic")
                counts.append(lease.tool_counts["catalog_lists"])
                retained = self.lifecycle.uid_processes(51003)
                self.assertEqual(len(retained), 2)
                for pid in retained:
                    self.assertIn("\nState:\tT", Path("/proc", str(pid), "status").read_text())
            self.assertGreater(counts[0], 0)
            self.assertEqual(counts, [counts[0]] * 3)
            with sessions.lease("42", {}, "synthetic-profile", "d" * 43, 20) as configured:
                changed = {**options, "config": {**options["config"],
                    "model_auto_compact_token_limit": 1000, "model_auto_compact_token_limit_scope": "total"}}
                thread = configured.resume(thread.id, **changed)
                self.assertEqual(configured.thread_action, "configure")
                configured.bind(thread, changed, "synthetic")
            self.assertEqual(configured.tool_counts["catalog_lists"], counts[0])
        finally:
            sessions.shutdown()
        self.assertEqual(self.lifecycle.uid_processes(51003), [])

    def test_persisted_stdio_history_migrates_replaces_helper_and_rolls_back(self):
        from openai_codex import Codex, CodexConfig, Sandbox, ApprovalMode
        from src.agent.runtime_session import RuntimeSessions
        from src.agent.request_tools import request_config, TOOLS
        from src.agent.instruction_refresh import refresh_instructions

        self.cleanup()
        root = self.root / "migration-native"
        root.mkdir(mode=0o755)
        (root / "long-retention").touch()
        sessions = RuntimeSessions(lambda: Codex(CodexConfig(launch_args_override=[
            sys.executable, "-u", "-c", HARNESS, str(root), "pinned-native"])))
        observed = root / "workspace" / "stdio-starts"
        program = ("from pathlib import Path; import asyncio,os; from taskopotamus_worker import request_mcp; "
                   "from taskopotamus_worker.request_scope import load_scope; "
                   "request_mcp.credentials=lambda: load_scope(os.environ['TASKOPOTAMUS_REQUEST_SCOPE'],runtime_gid=51003); "
                   f"Path({str(observed)!r}).open('a').write('start\\n'); asyncio.run(request_mcp.main())")
        def options(config):
            return {"cwd": str(root / "workspace"), "sandbox": Sandbox.full_access,
                    "approval_mode": ApprovalMode.deny_all,
                    "config": {"features.apps": False, "web_search": "disabled", **config}}
        legacy = options({"mcp_servers.request_tools": {
            "command": sys.executable, "args": ["-c", program],
            "env_vars": ["TASKOPOTAMUS_REQUEST_SCOPE"], "enabled_tools": list(TOOLS),
            "required": True, "tool_timeout_sec": 120,
            "tools": {name: {"approval_mode": "auto"} for name in TOOLS}}})
        marker = "offline-persisted-conversation-marker"
        try:
            with sessions.lease("42", {}, "synthetic-profile", "a" * 43, 20) as first:
                thread = first.client.thread_start(**legacy)
                identifier = thread.id
                refresh_instructions(thread, marker)
                first.parent_thread_id = identifier
            self.assertEqual(observed.read_text().splitlines(), ["start"])
            for token in ("b", "c"):
                with sessions.lease("42", {}, "synthetic-profile", token * 43, 20) as lease:
                    current = options(request_config(lease.tools))
                    thread = lease.resume(identifier, **current)
                    self.assertEqual(thread.id, identifier)
                    lease.bind(thread, current, "synthetic")
                self.assertGreater(lease.tool_counts["catalog_lists"], 0)
                self.assertEqual(observed.read_text().splitlines(), ["start"])
                sessions.close()  # Replace native plus helper without rewriting persisted state.
            with sessions.lease("42", {}, "synthetic-profile", "d" * 43, 20) as rollback:
                restored = rollback.resume(identifier, **legacy)
                self.assertEqual(restored.id, identifier)
                rollback.parent_thread_id = identifier
            self.assertEqual(observed.read_text().splitlines(), ["start", "start"])
            histories = list((root / "native" / "sessions").rglob("*.jsonl"))
            self.assertTrue(any(marker in path.read_text() for path in histories))
        finally:
            sessions.shutdown()
        self.assertEqual(self.lifecycle.uid_processes(51003), [])
