"""Offline packet decisions against generated filter rules; never modify host networking."""
import importlib.util
import ipaddress
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path("deploy/worker/firewall.py")
spec = importlib.util.spec_from_file_location("worker_firewall", SOURCE)
firewall = importlib.util.module_from_spec(spec)
spec.loader.exec_module(firewall)
MCP, AGENT, DNS = "10.96.3.18", "10.96.3.30", "10.96.0.10"


def verdict(commands, *, destination="127.0.0.1", port=15001, protocol="tcp", interface="eth0",
            original_destination="8.8.8.8", original_port=443, direction="ORIGINAL", dnat=True,
            state="NEW"):
    values = {"-p": protocol, "--dport": str(port), "-o": interface, "--ctdir": direction,
              "--ctorigdstport": str(original_port)}
    for rule in commands:
        if rule[:3] != ["iptables", "-A", "OUTPUT"]:
            continue
        matches = True
        for index in range(3, len(rule), 2):
            name, value = rule[index:index + 2]
            if name == "-j":
                if matches:
                    return value
            elif name in ("-d", "--ctorigdst"):
                address = destination if name == "-d" else original_destination
                matches &= ipaddress.ip_address(address) in ipaddress.ip_network(value)
            elif name == "--ctstate":
                matches &= bool(set(value.split(",")) & ({state, "DNAT"} if dnat else {state}))
            elif name == "--dports":
                matches &= str(port) in value.split(",")
            elif name != "-m":
                matches &= values[name] == value
    return "DROP"


class WorkerFirewallTests(unittest.TestCase):
    def setUp(self):
        self.rules = firewall.rules(MCP, DNS, AGENT, ambient=True)

    def test_redirected_dns_retains_original_resolver_and_port_boundary(self):
        for protocol in ("udp", "tcp"):
            args = dict(protocol=protocol, port=15053, original_destination=DNS, original_port=53)
            self.assertEqual(verdict(self.rules, **args), "ACCEPT")
            for change in ({"original_destination": "8.8.8.8"}, {"original_port": 54},
                           {"destination": "127.0.0.2"}, {"direction": "REPLY"}, {"dnat": False}):
                with self.subTest(protocol=protocol, change=change):
                    self.assertEqual(verdict(self.rules, **{**args, **change}), "REJECT")

    def test_redirected_peers_keep_exact_addresses_and_ports(self):
        for peer in (MCP, AGENT):
            self.assertEqual(verdict(self.rules, original_destination=peer, original_port=8080), "ACCEPT")
            for port in (53, 443, 5432, 8081):
                self.assertEqual(verdict(self.rules, original_destination=peer, original_port=port), "REJECT")
        self.assertEqual(verdict(self.rules, original_destination="10.96.3.99", original_port=8080), "REJECT")

    def test_redirected_public_web_rejects_original_blocked_networks_first(self):
        for port in (80, 443):
            self.assertEqual(verdict(self.rules, original_port=port), "ACCEPT")
            for network in firewall.BLOCKED_NETWORKS:
                with self.subTest(network=network, port=port):
                    address = str(next(ipaddress.ip_network(network).hosts()))
                    self.assertEqual(verdict(self.rules, original_destination=address, original_port=port), "REJECT")
        for port in (22, 53, 8080, 15001):
            self.assertEqual(verdict(self.rules, original_port=port), "REJECT")

    def test_direct_proxy_and_unproven_tuples_cannot_use_loopback_or_established_accept(self):
        for protocol, port in (("udp", 15053), ("tcp", 15053), ("tcp", 15001)):
            for destination in ("127.0.0.1", "127.0.0.2", "10.244.1.10"):
                for state in ("NEW", "ESTABLISHED"):
                    with self.subTest(protocol=protocol, port=port, destination=destination, state=state):
                        self.assertEqual(verdict(self.rules, protocol=protocol, port=port, destination=destination,
                                                 interface="lo", dnat=False, state=state), "REJECT")
        self.assertEqual(verdict(self.rules, direction="REPLY"), "REJECT")
        self.assertEqual(verdict(self.rules, destination="10.244.1.10"), "REJECT")

    def test_established_remote_replies_preserve_unrestricted_client_source_ports(self):
        legacy = firewall.rules(MCP, DNS, AGENT)
        for protocol, port in (("udp", 15053), ("tcp", 15053), ("tcp", 15001)):
            for state in ("ESTABLISHED", "RELATED"):
                packet = dict(protocol=protocol, port=port, destination="10.244.1.20", original_port=2222,
                              direction="REPLY", dnat=False, state=state)
                with self.subTest(protocol=protocol, port=port, state=state):
                    self.assertEqual(verdict(legacy, **packet), "ACCEPT")
                    self.assertEqual(verdict(self.rules, **packet), "ACCEPT")
                    self.assertEqual(verdict(self.rules, **{**packet, "direction": "ORIGINAL"}), "REJECT")
                    self.assertEqual(verdict(self.rules, **{**packet, "state": "NEW"}), "REJECT")

    def test_legacy_rules_and_nonproxy_paths_are_preserved(self):
        legacy = firewall.rules(MCP, DNS, AGENT)
        self.assertEqual(legacy, firewall.rules(MCP, DNS, AGENT, ambient=False))
        loopback = self.rules.index(["iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"])
        self.assertEqual(self.rules[:2] + self.rules[loopback:], legacy)
        self.assertEqual(self.rules[-1], ["ip6tables", "-P", "OUTPUT", "DROP"])
        self.assertTrue(all(command[0] in ("iptables", "ip6tables") and "-t" not in command for command in self.rules))
        for packet in (dict(destination=DNS, port=53, protocol="udp"), dict(destination=AGENT, port=8080),
                       dict(destination=MCP, port=8080), dict(destination="8.8.8.8", port=443),
                       dict(destination="127.0.0.1", port=55555, interface="lo"),
                       dict(destination="10.96.3.99", port=55555, state="ESTABLISHED"),
                       dict(destination="169.254.169.254", port=443)):
            self.assertEqual(verdict(self.rules, **packet), verdict(legacy, **packet))

    def test_cli_rejects_invalid_mode_before_applying_rules(self):
        for value in ("TRUE", "1", "", "yes"):
            with patch("sys.argv", [str(SOURCE), MCP, DNS, AGENT, value]), \
                    patch("subprocess.run") as apply, self.assertRaises(ValueError):
                runpy.run_path(str(SOURCE), run_name="__main__")
            apply.assert_not_called()
        for value in (True, False):
            with patch("sys.argv", [str(SOURCE), MCP, DNS, AGENT, str(value).lower()]), \
                    patch("subprocess.run") as apply:
                runpy.run_path(str(SOURCE), run_name="__main__")
            self.assertEqual([call.args[0] for call in apply.call_args_list],
                             firewall.rules(MCP, DNS, AGENT, ambient=value))
        with self.assertRaises(ValueError):
            firewall.rules(MCP, DNS, AGENT, ambient="false")

    def test_startup_passes_derived_flag_before_dropping_firewall_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            startup = Path("deploy/worker/start_worker.sh").read_text()
            self.assertIn("exec setpriv --bounding-set=-net_admin", startup)
            mocks = "\n".join(name + '() { printf "%s\\n" "' + name + ' $*" >> "$CAPTURE"; }'
                              for name in ("mkdir", "chmod", "cp", "python", "setpriv"))
            # Intercept the final exec as well, so no real paths, credentials or services are touched.
            program = mocks + "\n" + startup.replace("exec setpriv ", "setpriv ", 1)
            for value in (None, "false", "true"):
                capture = root / "commands"
                capture.unlink(missing_ok=True)
                environment = {"PATH": str(root), "CAPTURE": str(capture), "WORKER_MCP_IP": MCP,
                               "WORKER_DNS_IP": DNS, "WORKER_AGENT_IP": AGENT}
                if value is not None:
                    environment["WORKER_AMBIENT"] = value
                result = subprocess.run(["/bin/sh", "-c", program], env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = capture.read_text().splitlines()
                firewall_call = next(i for i, call in enumerate(calls) if "/worker-firewall " in call)
                drop_call = next(i for i, call in enumerate(calls) if "--bounding-set=-net_admin" in call)
                self.assertLess(firewall_call, drop_call)
                self.assertTrue(calls[firewall_call].endswith(f"{MCP} {DNS} {AGENT} {value or 'false'}"))
