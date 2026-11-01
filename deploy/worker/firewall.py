import ipaddress
import subprocess
import sys


BLOCKED_NETWORKS = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)


def rules(mcp_ip, dns_ip, agent_ip="10.203.17.30", *, ambient=False):
    if type(ambient) is not bool:
        raise ValueError("Worker ambient mode must be a boolean.")
    address = ipaddress.IPv4Address(mcp_ip)
    if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
        raise ValueError("Worker MCP address must be a private service IPv4 address.")
    agent = ipaddress.IPv4Address(agent_ip)
    if not agent.is_private or agent.is_loopback or agent.is_link_local or agent.is_unspecified or agent == address:
        raise ValueError("Worker attachment address must be a separate private service IPv4 address.")
    resolver = ipaddress.IPv4Address(dns_ip)
    if resolver.is_multicast or resolver.is_unspecified or resolver.is_reserved or resolver.is_link_local or resolver.is_loopback:
        raise ValueError("Worker DNS resolver must be a unicast service IPv4 address.")
    commands = [
        ["iptables", "-P", "OUTPUT", "DROP"],
        ["iptables", "-F", "OUTPUT"],
    ]
    if ambient:
        commands.append(["iptables", "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
                         "--ctdir", "REPLY", "-j", "ACCEPT"])
        # CNI redirects precede filter OUTPUT; authorize the kernel's original tuple, not its proxy.
        for protocol, port in (("udp", "15053"), ("tcp", "15053"), ("tcp", "15001")):
            listener = ["iptables", "-A", "OUTPUT", "-p", protocol, "--dport", port]
            original = listener + ["-d", "127.0.0.1", "-m", "conntrack", "--ctstate", "DNAT",
                                   "--ctdir", "ORIGINAL"]
            if port == "15053":
                commands.append(original + ["--ctorigdst", str(resolver), "--ctorigdstport", "53", "-j", "ACCEPT"])
            else:
                commands.extend(original + ["--ctorigdst", str(peer), "--ctorigdstport", "8080", "-j", "ACCEPT"]
                                for peer in (address, agent))
                commands.extend(original + ["--ctorigdst", network, "-j", "REJECT"] for network in BLOCKED_NETWORKS)
                commands.extend(original + ["--ctorigdstport", web_port, "-j", "ACCEPT"] for web_port in ("80", "443"))
            commands.append(listener + ["-j", "REJECT"])
    commands.extend([
        ["iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
        ["iptables", "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ["iptables", "-A", "OUTPUT", "-p", "tcp", "-d", str(address), "--dport", "8080", "-j", "ACCEPT"],
        ["iptables", "-A", "OUTPUT", "-p", "tcp", "-d", str(agent), "--dport", "8080", "-j", "ACCEPT"],
    ])
    commands.extend(["iptables", "-A", "OUTPUT", "-d", str(resolver), "-p", protocol,
                     "--dport", "53", "-j", "ACCEPT"] for protocol in ("udp", "tcp"))
    commands.extend(["iptables", "-A", "OUTPUT", "-d", network, "-j", "REJECT"] for network in BLOCKED_NETWORKS)
    commands.append(["iptables", "-A", "OUTPUT", "-p", "tcp", "-m", "multiport", "--dports", "80,443", "-j", "ACCEPT"])
    commands.append(["ip6tables", "-P", "OUTPUT", "DROP"])
    return commands


if __name__ == "__main__":
    ambient = sys.argv[4] if len(sys.argv) == 5 else "false"
    if len(sys.argv) not in (4, 5) or ambient not in ("true", "false"):
        raise ValueError("Expected worker addresses and optional true/false ambient mode.")
    for command in rules(sys.argv[1], sys.argv[2], sys.argv[3], ambient=ambient == "true"):
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
