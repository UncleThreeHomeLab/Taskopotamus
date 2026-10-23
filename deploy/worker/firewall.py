import ipaddress
import subprocess
import sys


BLOCKED_NETWORKS = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)


def rules(mcp_ip, dns_ip):
    address = ipaddress.IPv4Address(mcp_ip)
    if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
        raise ValueError("Worker MCP address must be a private service IPv4 address.")
    resolver = ipaddress.IPv4Address(dns_ip)
    if resolver.is_multicast or resolver.is_unspecified or resolver.is_reserved or resolver.is_link_local or resolver.is_loopback:
        raise ValueError("Worker DNS resolver must be a unicast service IPv4 address.")
    commands = [
        ["iptables", "-P", "OUTPUT", "DROP"],
        ["iptables", "-F", "OUTPUT"],
        ["iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
        ["iptables", "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ["iptables", "-A", "OUTPUT", "-p", "tcp", "-d", str(address), "--dport", "8080", "-j", "ACCEPT"],
    ]
    commands.extend(["iptables", "-A", "OUTPUT", "-d", str(resolver), "-p", protocol,
                     "--dport", "53", "-j", "ACCEPT"] for protocol in ("udp", "tcp"))
    commands.extend(["iptables", "-A", "OUTPUT", "-d", network, "-j", "REJECT"] for network in BLOCKED_NETWORKS)
    commands.append(["iptables", "-A", "OUTPUT", "-p", "tcp", "-m", "multiport", "--dports", "80,443", "-j", "ACCEPT"])
    commands.append(["ip6tables", "-P", "OUTPUT", "DROP"])
    return commands


if __name__ == "__main__":
    for command in rules(sys.argv[1], sys.argv[2]):
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
