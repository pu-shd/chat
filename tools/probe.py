#!/usr/bin/env python3
"""probe.py — small, testable probes for keepalive.zsh.

    probe.py cert-days <host>            days until the TLS certificate at host:443 expires
    probe.py days-until <ISO timestamp>  days from now until a timestamp (Key Vault "expires")
    probe.py cidrs [--min-prefix N] [--max-count N] < ranges
                                         validate an allowlist: IPv4, strict CIDR (a bare
                                         address becomes /32), no wider than /N (default 8),
                                         at most N entries (default 200); prints them normalised

Prints an integer. Exit 2 when the host cannot be reached or the input is malformed.
CHAT_PROBE_FIXTURE (JSON {"host": days}) replaces the network for tests.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import ssl
import sys


def cert_days(host: str, port: int = 443, timeout: float = 15.0) -> int:
    fixture = os.environ.get("CHAT_PROBE_FIXTURE")
    if fixture:
        table = json.loads(fixture)
        if host not in table:
            raise OSError(f"{host}: not in CHAT_PROBE_FIXTURE")
        return int(table[host])
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            not_after = tls.getpeercert()["notAfter"]
    expires = dt.datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), dt.timezone.utc)
    return (expires - dt.datetime.now(dt.timezone.utc)).days


def days_until(stamp: str) -> int:
    when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return (when - dt.datetime.now(dt.timezone.utc)).days


def cidrs(lines: list[str], min_prefix: int = 8, max_count: int = 200) -> list[str]:
    import ipaddress
    out = []
    for raw in lines:
        item = raw.strip()
        if not item:
            continue
        net = ipaddress.ip_network(item if "/" in item else f"{item}/32", strict=True)
        if net.version != 4:
            raise ValueError(f"{item}: not IPv4")
        if net.prefixlen < min_prefix:
            raise ValueError(f"{item}: wider than /{min_prefix}")
        out.append(str(net))
    if not out:
        raise ValueError("no ranges")
    if len(out) > max_count:
        raise ValueError(f"{len(out)} ranges exceeds the limit of {max_count}")
    return sorted(set(out), key=lambda n: tuple(int(x) for x in n.replace("/", ".").split(".")))


def main(argv: list[str]) -> int:
    try:
        if argv and argv[0] == "cidrs":
            import argparse
            ap = argparse.ArgumentParser(prog="probe.py cidrs")
            ap.add_argument("--min-prefix", type=int, default=8)
            ap.add_argument("--max-count", type=int, default=200)
            a = ap.parse_args(argv[1:])
            print("\n".join(cidrs(sys.stdin.read().splitlines(), a.min_prefix, a.max_count)))
            return 0
        if len(argv) == 2 and argv[0] == "cert-days":
            print(cert_days(argv[1]))
        elif len(argv) == 2 and argv[0] == "days-until":
            print(days_until(argv[1]))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (OSError, ssl.SSLError, ValueError, KeyError) as exc:
        print(f"probe: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
