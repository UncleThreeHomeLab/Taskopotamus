"""Offline assertions against rendered Kubernetes resources, never a cluster."""
import ast
import ipaddress
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy/helm/taskopotamus"
FIXTURE = ROOT / "tests/fixtures/helm-values.yaml"
NAMESPACE = "taskopotamus-tests"
RELEASE = "synthetic"


def render(overrides=None, *, fixture=True, command="template", namespace=NAMESPACE):
    with tempfile.TemporaryDirectory(prefix="taskopotamus-helm-") as directory:
        environment = {
            **os.environ,
            "KUBECONFIG": os.devnull,
            "HELM_CACHE_HOME": directory + "/cache",
            "HELM_CONFIG_HOME": directory + "/config",
            "HELM_DATA_HOME": directory + "/data",
            "HELM_PLUGINS": directory + "/plugins",
        }
        arguments = ["helm", command]
        arguments += [RELEASE, str(CHART)] if command == "template" else [str(CHART), "--strict"]
        arguments += ["--namespace", namespace]
        if fixture:
            arguments += ["-f", str(FIXTURE)]
        if overrides:
            values = Path(directory) / "values.yaml"
            values.write_text(yaml.safe_dump(overrides), encoding="utf-8")
            arguments += ["-f", str(values)]
        return subprocess.run(arguments, env=environment, capture_output=True, text=True, timeout=30)


def resources(overrides=None):
    result = render(overrides)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def by_kind(items, kind):
    return {item["metadata"]["name"]: item for item in items if item["kind"] == kind}


def labels(component):
    return {"app.kubernetes.io/name": "taskopotamus", "app.kubernetes.io/instance": RELEASE,
            "app.kubernetes.io/component": component}


def matches(selector, actual):
    return all(actual.get(key) == value for key, value in selector.get("matchLabels", {}).items())


def permits(policies, component, direction, peer_component, port, *, protocol="TCP", ip="10.244.1.10",
            namespace=NAMESPACE, peer_labels=None, namespace_labels=None):
    """Evaluate the label/IP/port forms emitted by this chart using NetworkPolicy union rules."""
    selected = [policy["spec"] for policy in policies if direction in policy["spec"]["policyTypes"]
                and matches(policy["spec"]["podSelector"], labels(component))]
    if not selected:
        return True
    peer_labels = labels(peer_component) if peer_labels is None else peer_labels
    namespace_labels = {"kubernetes.io/metadata.name": namespace} if namespace_labels is None else namespace_labels
    field, peers = ("egress", "to") if direction == "Egress" else ("ingress", "from")
    for policy in selected:
        for rule in policy.get(field, []):
            if not any(entry["port"] == port and entry.get("protocol", "TCP") == protocol
                       for entry in rule.get("ports", [])):
                continue
            for peer in rule.get(peers, []):
                if "ipBlock" in peer:
                    block = peer["ipBlock"]
                    address = ipaddress.ip_address(ip)
                    if address in ipaddress.ip_network(block["cidr"]) and not any(
                        address in ipaddress.ip_network(network) for network in block.get("except", [])
                    ):
                        return True
                elif (namespace == NAMESPACE if "namespaceSelector" not in peer else
                      matches(peer["namespaceSelector"], namespace_labels)) and matches(
                          peer.get("podSelector", {}), peer_labels):
                    return True
    return False


class HelmChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("helm"):
            raise RuntimeError("The offline test image must supply Helm.")
        cls.items = resources()
        cls.deployments = by_kind(cls.items, "Deployment")
        cls.services = by_kind(cls.items, "Service")

    def test_lint_and_required_input_rejection(self):
        result = render(command="lint")
        self.assertEqual(result.returncode, 0, result.stderr)
        invalid = [
            {"network": {"agentServiceIP": "127.0.0.1"}},
            {"network": {"agentServiceIP": "10.999.0.1"}},
            {"network": {"agentServiceIP": "10.96.3.18"}},
            {"network": {"dnsIP": "10.96.3.30"}},
            {"network": {"clusterCIDRs": []}},
            {"network": {"clusterCIDRs": ["not-a-cidr"]}},
            {"telegram": {"allowedUserIds": []}},
            {"worker": {"privileged": True}},
            {"agent": {"turnTimeoutSeconds": 29}},
            {"agent": {"artifacts": {"maxFiles": 11}}},
            {"agent": {"artifacts": {"inputMaxBytes": 20000001}}},
            {"agent": {"artifacts": {"outputMaxBytes": 50000001}}},
            {"agent": {"artifacts": {"imageMaxBytes": 10000001}}},
            {"agent": {"artifacts": {"stagingSeconds": 101, "retentionSeconds": 100}}},
            {"resources": {"worker": {"limits": {"memory": "0"}}}},
            {"storage": {"existingClaims": {"native": "shared", "saved": "shared"}}},
            {"storage": {"legacyClaim": "synthetic-state"}},
        ]
        for values in invalid:
            with self.subTest(values=values):
                self.assertNotEqual(render(values).returncode, 0)
        self.assertNotEqual(render(fixture=False).returncode, 0)
        self.assertNotEqual(render(namespace="default").returncode, 0)

    def test_services_match_private_workloads_and_endpoint_pins(self):
        self.assertEqual(set(self.deployments), {"postgres", "agent", "mcp", "google", "telegram", "worker"})
        self.assertEqual(set(self.services), set(self.deployments) - {"telegram"})
        expected_ports = {"postgres": [5432], "agent": [8080], "mcp": [8080], "google": [8080, 8081, 8082], "worker": [2222]}
        for name, service in self.services.items():
            self.assertEqual(service["spec"]["type"], "ClusterIP")
            self.assertEqual(service["spec"]["selector"], self.deployments[name]["spec"]["template"]["metadata"]["labels"])
            self.assertEqual([port["port"] for port in service["spec"]["ports"]], expected_ports[name])
            self.assertTrue(all(port["port"] == port["targetPort"] for port in service["spec"]["ports"]))
        fixture = yaml.safe_load(FIXTURE.read_text())["network"]
        self.assertEqual(self.services["agent"]["spec"]["clusterIP"], fixture["agentServiceIP"])
        self.assertEqual(self.services["mcp"]["spec"]["clusterIP"], fixture["mcpServiceIP"])
        worker = self.deployments["worker"]["spec"]["template"]["spec"]
        environment = {item["name"]: item.get("value") for item in worker["containers"][0]["env"]}
        self.assertEqual(environment["WORKER_AGENT_IP"], fixture["agentServiceIP"])
        self.assertEqual(environment["WORKER_MCP_IP"], fixture["mcpServiceIP"])
        self.assertEqual(environment["WORKER_DNS_IP"], fixture["dnsIP"])
        self.assertEqual(worker["dnsPolicy"], "None")
        self.assertEqual(worker["dnsConfig"]["nameservers"], [fixture["dnsIP"]])
        self.assertIn(NAMESPACE + ".svc.cluster.local", worker["dnsConfig"]["searches"])

    def test_single_instances_and_security_contexts(self):
        capabilities = {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "SYS_CHROOT", "SETPCAP", "NET_ADMIN", "KILL"}
        for name, deployment in self.deployments.items():
            self.assertEqual(deployment["spec"]["replicas"], 1)
            self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
            pod = deployment["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertFalse(pod["enableServiceLinks"])
            self.assertFalse(any(pod.get(key, False) for key in ("hostNetwork", "hostPID", "hostIPC")))
            self.assertNotIn("fsGroup", pod["securityContext"])
            expected_uid = 0 if name == "worker" else 999 if name == "postgres" else 10001
            self.assertEqual(pod["securityContext"]["runAsUser"], expected_uid)
            container = pod["containers"][0]
            security = container["securityContext"]
            self.assertTrue(security["readOnlyRootFilesystem"])
            self.assertFalse(security["allowPrivilegeEscalation"])
            self.assertFalse(security.get("privileged", False))
            self.assertEqual(security["capabilities"]["drop"], ["ALL"])
            self.assertEqual(set(security["capabilities"].get("add", [])), capabilities if name == "worker" else set())
            if name != "postgres":
                self.assertEqual(container["command"], ["/usr/bin/tini", "-g", "--"])
            if name != "telegram":
                self.assertIn("exec", container["readinessProbe"])
                self.assertIn("exec", container["startupProbe"])
            compile(pod["initContainers"][0]["command"][2], "<volume-initializer>", "exec")
        self.assertEqual(self.deployments["worker"]["spec"]["template"]["spec"]["securityContext"]["sysctls"],
                         [{"name": "net.ipv6.conf.all.disable_ipv6", "value": "1"}])

    def test_secret_item_separation_and_read_only_private_copies(self):
        expected = {
            "postgres": {"postgres_admin_password", "agent_database_password", "profile_database_password", "telegram_database_password"},
            "agent": {"agent_api_token", "profile_signing_key", "google_data_secret", "agent_database_password", "worker_client_key", "worker_host_key.pub"},
            "mcp": {"profile_signing_key", "profile_database_password"},
            "google": {"google_control_secret", "google_data_secret", "google_token_encryption_key"},
            "telegram": {"agent_api_token", "google_control_secret", "telegram_database_password"},
            "worker": {"worker_host_key", "worker_client_key.pub"},
        }
        self.assertFalse(by_kind(self.items, "Secret"))
        for name, deployment in self.deployments.items():
            pod = deployment["spec"]["template"]["spec"]
            volumes = {item["name"]: item for item in pod["volumes"]}
            projected = volumes["projected-secrets"]["secret"]
            self.assertEqual(projected["secretName"], "synthetic-taskopotamus")
            self.assertEqual(projected["defaultMode"], 0o400)
            self.assertEqual({item["key"] for item in projected["items"]}, expected[name])
            mounts = {item["name"]: item for item in pod["containers"][0]["volumeMounts"]}
            self.assertTrue(mounts["projected-secrets" if name == "worker" else "private-secrets"]["readOnly"])
            if name != "worker":
                self.assertNotIn("projected-secrets", mounts)
                self.assertEqual(volumes["private-secrets"]["emptyDir"]["medium"], "Memory")
        environment = self.deployments["telegram"]["spec"]["template"]["spec"]["containers"][0]["env"]
        token = next(item for item in environment if item["name"] == "TELEGRAM_BOT_TOKEN")
        self.assertEqual(token["valueFrom"]["secretKeyRef"], {"name": "synthetic-taskopotamus", "key": "telegram_bot_token"})

    @unittest.skipUnless(os.name != "nt" and getattr(os, "geteuid", lambda: -1)() == 0,
                         "Run volume ownership checks in the offline root test pass")
    def test_volume_initializers_preserve_contents_and_private_ownership(self):
        for name, deployment in self.deployments.items():
            with self.subTest(component=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pod = deployment["spec"]["template"]["spec"]
                initializer = pod["initContainers"][0]

                def isolated_path(*parts):
                    path = Path(*parts)
                    target = path if path.is_relative_to(root) else root / path.relative_to("/")
                    if not target.resolve().is_relative_to(root):
                        raise AssertionError("Rendered initializer escaped isolated test storage.")
                    return target

                def chown(path, uid, gid):
                    os.chown(isolated_path(path), uid, gid)

                for mount in initializer["volumeMounts"]:
                    isolated_path(mount["mountPath"]).mkdir(parents=True, exist_ok=True)
                sentinel = root / "retained"
                sentinel.mkdir()
                for mount in initializer["volumeMounts"]:
                    if mount["mountPath"].startswith("/data/"):
                        sentinel = isolated_path(mount["mountPath"]) / "retained"
                        sentinel.write_bytes(b"unchanged historical data")
                        os.chown(sentinel, 51001, 51002)
                        sentinel.chmod(0o640)
                before = (sentinel.stat().st_uid, sentinel.stat().st_gid, sentinel.stat().st_mode, sentinel.read_bytes()) if sentinel.is_file() else None
                projected = next(volume["secret"] for volume in pod["volumes"] if "secret" in volume)
                if name != "worker":
                    for item in projected["items"]:
                        isolated_path("/secret-source", item["path"]).write_bytes(b"synthetic:" + item["path"].encode())
                # Substitute filesystem entry points, then execute the actual rendered initializer.
                tree = ast.parse(initializer["command"][2])
                tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
                exec(compile(tree, "<rendered-volume-initializer>", "exec"),
                     {"__builtins__": {}, "Path": isolated_path, "os": SimpleNamespace(chown=chown)})
                if before:
                    self.assertEqual((sentinel.stat().st_uid, sentinel.stat().st_gid, sentinel.stat().st_mode, sentinel.read_bytes()), before)
                if name != "worker":
                    uid = 999 if name == "postgres" else 10001
                    for mount in initializer["volumeMounts"]:
                        if mount["mountPath"].startswith("/data/"):
                            info = isolated_path(mount["mountPath"]).stat()
                            self.assertEqual((info.st_uid, info.st_gid, info.st_mode & 0o7777), (uid, uid, 0o700))
                    self.assertEqual(isolated_path("/tmp").stat().st_mode & 0o7777, 0o1777)
                    if name == "postgres":
                        info = isolated_path("/var/run/postgresql").stat()
                        self.assertEqual((info.st_uid, info.st_gid, info.st_mode & 0o7777), (999, 999, 0o3770))
                    target = isolated_path("/run/secrets")
                    self.assertEqual((target.stat().st_uid, target.stat().st_gid, target.stat().st_mode & 0o777), (uid, uid, 0o700))
                    for item in projected["items"]:
                        path = target / item["path"]
                        self.assertEqual((path.stat().st_uid, path.stat().st_gid, path.stat().st_mode & 0o777), (uid, uid, 0o400))
                        self.assertEqual(path.read_bytes(), b"synthetic:" + item["path"].encode())
                else:
                    for path in ("/tmp", "/run", "/root", "/home/agent"):
                        self.assertEqual(isolated_path(path).stat().st_mode & 0o777, 0o700)

    def test_retained_single_writer_claims_and_exact_worker_mounts(self):
        claims = by_kind(self.items, "PersistentVolumeClaim")
        self.assertEqual(len(claims), 7)
        for claim in claims.values():
            self.assertEqual(claim["metadata"]["annotations"]["helm.sh/resource-policy"], "keep")
            self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteOncePod"])
        pod = self.deployments["worker"]["spec"]["template"]["spec"]
        mounts = {item["name"]: item for item in pod["containers"][0]["volumeMounts"]}
        expected = {"native": "/home/agent/.codex", "workspace": "/workspace", "saved": "/saved", "state": "/var/lib/taskopotamus", "legacy": "/legacy-workspace"}
        self.assertEqual({name: mounts[name]["mountPath"] for name in expected}, expected)
        self.assertTrue(mounts["legacy"]["readOnly"])
        self.assertEqual(next(item for item in pod["volumes"] if item["name"] == "legacy")["emptyDir"], {})
        for name in ("tmp", "run", "root", "home"):
            self.assertEqual(next(item for item in pod["volumes"] if item["name"] == name)["emptyDir"]["medium"], "Memory")

    def test_existing_claims_optional_components_and_instructions(self):
        overrides = {
            "storage": {"existingClaims": {"native": "existing-native"}, "legacyClaim": "existing-legacy"},
            "objects": {"enabled": True}, "console": {"enabled": True},
            "google": {"oauthEnabled": True}, "instructionsConfigMap": "owner-instructions",
        }
        items = resources(overrides)
        deployments = by_kind(items, "Deployment")
        self.assertEqual(set(deployments) - set(self.deployments), {"objects", "test-console"})
        self.assertNotIn("synthetic-native", by_kind(items, "PersistentVolumeClaim"))
        worker = deployments["worker"]["spec"]["template"]["spec"]
        volumes = {item["name"]: item for item in worker["volumes"]}
        self.assertEqual(volumes["native"]["persistentVolumeClaim"]["claimName"], "existing-native")
        self.assertEqual(volumes["legacy"]["persistentVolumeClaim"], {"claimName": "existing-legacy", "readOnly": True})
        for name in ("agent", "mcp"):
            pod = deployments[name]["spec"]["template"]["spec"]
            mount = next(item for item in pod["containers"][0]["volumeMounts"] if item["name"] == "instructions")
            self.assertEqual(mount["mountPath"], "/app/config/agent")
            self.assertTrue(mount["readOnly"])
            self.assertNotIn("subPath", mount)
        policies = [item for item in items if item["kind"] == "NetworkPolicy"]
        self.assertTrue(permits(policies, "test-console", "Egress", "agent", 8080))
        self.assertFalse(permits(policies, "test-console", "Egress", "postgres", 5432))
        keys = next(volume["secret"]["items"] for volume in deployments["google"]["spec"]["template"]["spec"]["volumes"] if "secret" in volume)
        self.assertTrue({"google_oauth_client_id", "google_oauth_client_secret", "google_oauth_redirect_uri"}.issubset({item["key"] for item in keys}))

    def test_postgres_initialization_matches_canonical_asset(self):
        config = by_kind(self.items, "ConfigMap")["synthetic-postgres-init"]
        self.assertEqual(config["data"]["initialize_databases.sh"].strip(), (ROOT / "deploy/postgres/initialize_databases.sh").read_text().strip())
        container = self.deployments["postgres"]["spec"]["template"]["spec"]["containers"][0]
        environment = {item["name"]: item["value"] for item in container["env"]}
        self.assertEqual(environment["PGDATA"], "/var/lib/postgresql/18/docker")
        self.assertEqual(environment["POSTGRES_INITDB_ARGS"], "--auth-host=scram-sha-256")
        self.assertIn("log_min_error_statement=panic", container["args"])

    def test_network_policies_allow_only_required_component_paths(self):
        policies = [item for item in self.items if item["kind"] == "NetworkPolicy"]
        allowed = [
            ("telegram", "agent", 8080), ("telegram", "google", 8081), ("telegram", "postgres", 5432),
            ("agent", "worker", 2222), ("agent", "mcp", 8080), ("agent", "google", 8082), ("agent", "postgres", 5432),
            ("worker", "agent", 8080), ("worker", "mcp", 8080), ("google", "agent", 8080), ("mcp", "postgres", 5432),
        ]
        for source, target, port in allowed:
            with self.subTest(source=source, target=target, port=port):
                self.assertTrue(permits(policies, source, "Egress", target, port))
                self.assertTrue(permits(policies, target, "Ingress", source, port))
                self.assertFalse(permits(policies, target, "Ingress", source, port, namespace="another-namespace"))
        forbidden = [("worker", "postgres", 5432), ("worker", "google", 8082), ("mcp", "agent", 8080),
                     ("google", "postgres", 5432), ("telegram", "worker", 2222), ("telegram", "google", 8082)]
        for source, target, port in forbidden:
            with self.subTest(source=source, target=target, port=port):
                self.assertFalse(permits(policies, source, "Egress", target, port))

    def test_public_egress_excludes_cluster_private_and_metadata_addresses(self):
        policies = [item for item in self.items if item["kind"] == "NetworkPolicy"]
        self.assertFalse(permits(policies, "unrecognized", "Egress", "external", 443, ip="8.8.8.8", namespace="external"))
        self.assertFalse(permits(policies, "unrecognized", "Ingress", "agent", 8080))
        for component in ("worker", "google", "telegram"):
            self.assertTrue(permits(policies, component, "Egress", "external", 443, ip="8.8.8.8", namespace="external"))
            for address in ("10.96.3.30", "10.244.1.9", "192.168.1.2", "169.254.169.254", "127.0.0.1", "100.64.0.1"):
                with self.subTest(component=component, address=address):
                    self.assertFalse(permits(policies, component, "Egress", "external", 443, ip=address, namespace="external"))
        for component in ("agent", "mcp", "postgres"):
            self.assertFalse(permits(policies, component, "Egress", "external", 443, ip="8.8.8.8", namespace="external"))
        dns = dict(protocol="UDP", ip="10.96.0.10", namespace="kube-system", peer_labels={"k8s-app": "kube-dns"})
        self.assertTrue(permits(policies, "worker", "Egress", "dns", 53, **dns))
        self.assertFalse(permits(policies, "worker", "Egress", "dns", 53, **{**dns, "namespace": "other"}))
        self.assertFalse(permits(policies, "worker", "Egress", "external", 53, protocol="UDP", ip="8.8.8.8", namespace="external"))
