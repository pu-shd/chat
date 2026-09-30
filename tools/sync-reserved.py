#!/usr/bin/env python3
"""Regenerate tools/zulip_reserved.py from a Zulip release (run when bumping Zulip).

    tools/sync-reserved.py 12.3

Vendors ZULIP_RESERVED_SUBDOMAINS and GENERIC_RESERVED_SUBDOMAINS from
zerver/lib/name_restrictions.py, and the first path segments Zulip routes (from
zproject/urls.py, plus nginx-only locations), which a /<slug> redirect must not shadow.
"""
import ast
import re
import sys
import urllib.request
from pathlib import Path

NGINX_AND_SOCIAL = ["static", "local-static", "help", "thumbnail", "avatar", "internal", "complete",
                    "oauth", "devtools", "config-error", "self-hosting", "activity", "billing", "upgrade",
                    "sponsorship", "confirm", "emails", "subscriptions", "robots.txt", "favicon.ico",
                    ".well-known", "auth"]


def fetch(version: str, path: str) -> str:
    url = f"https://raw.githubusercontent.com/zulip/zulip/{version}/{path}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode()


def main(version: str) -> None:
    tree = ast.parse(fetch(version, "zerver/lib/name_restrictions.py"))
    sets = {n.targets[0].id: sorted(ast.literal_eval(n.value)) for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id in ("ZULIP_RESERVED_SUBDOMAINS", "GENERIC_RESERVED_SUBDOMAINS")}
    if len(sets) != 2:
        sys.exit("name_restrictions.py changed shape; update this script")
    urls = fetch(version, "zproject/urls.py")
    routes = sorted(set(re.findall(r'(?:re_)?path\(\s*"\^?/?([a-z0-9_.-]+)', urls)) | set(NGINX_AND_SOCIAL))
    out = [f'"""Vendored from zulip/zulip@{version} zerver/lib/name_restrictions.py by tools/sync-reserved.py. Do not edit."""',
           "", f'ZULIP_VERSION = "{version}"', ""]
    for name, items in sets.items():
        out += [f"{name} = frozenset({{", *[f"    {x!r}," for x in items], "})", ""]
    out += [f"# First path segments routed by Zulip {version} (zproject/urls.py + nginx app locations).",
            "# A /<slug> redirect on these would shadow a Zulip route.",
            "ZULIP_ROUTE_SEGMENTS = frozenset({", *[f"    {x!r}," for x in routes], "})", ""]
    Path(__file__).with_name("zulip_reserved.py").write_text("\n".join(out))
    print(f"zulip_reserved.py: {sum(map(len, sets.values()))} reserved names, {len(routes)} routes from Zulip {version}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__))
