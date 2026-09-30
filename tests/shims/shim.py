#!/usr/bin/env python3
"""Recording stand-in for az / gh / cosign / dig / curl in script tests.

Symlinked under each command's name. Behaviour comes from $SHIM_SPEC (JSON):

    {"az": [{"match": "<regex over the joined argv>", "stdout": "...", "exit": 0,
             "files": {"{dir}/gateways.json": "/path/to/fixture"}}, ...], "gh": [...]}

The first rule whose regex matches wins ("times": N limits a rule to its first N matches). A call that matches nothing fails loudly
(exit 97) so a test can never pass because a command was silently ignored. Every call
is appended to $SHIM_LOG as JSON: {"cmd", "args", "file": <contents of any --file arg>,
"at_files": {<path>: <contents> for every @path argument}}.
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

cmd = Path(sys.argv[0]).name
args = sys.argv[1:]
joined = " ".join(args)

entry = {"cmd": cmd, "args": args}
if "--file" in args:
    try:
        entry["file"] = Path(args[args.index("--file") + 1]).read_text()
    except (IndexError, OSError):
        entry["file"] = None
at_files = {}
for a in args:
    if a.startswith("@") and Path(a[1:]).is_file():
        at_files[a[1:]] = Path(a[1:]).read_text()
if at_files:
    entry["at_files"] = at_files
if not sys.stdin.isatty() and ((cmd == "gh" and "secret" in args) or (cmd == "curl" and "-K" in args)):
    entry["stdin"] = sys.stdin.read()
    joined += " " + entry["stdin"].strip()  # curl -K - carries the URL on stdin
with open(os.environ["SHIM_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")

spec = json.loads(Path(os.environ["SHIM_SPEC"]).read_text())
for idx, rule in enumerate(spec.get(cmd, [])):
    if re.search(rule["match"], joined):
        if "times" in rule:  # match only the first N calls, then fall through
            counter = Path(os.environ["SHIM_LOG"] + f".{cmd}.{idx}.count")
            used = int(counter.read_text()) if counter.exists() else 0
            if used >= rule["times"]:
                continue
            counter.write_text(str(used + 1))
        def argval(flag):
            return args[args.index(flag) + 1] if flag in args else ""
        for dest, src in rule.get("files", {}).items():
            target = Path(dest.replace("{dir}", argval("--dir")).replace("{o}", argval("-o")))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        out = rule.get("stdout", "")
        if out:
            sys.stdout.write(out if out.endswith("\n") else out + "\n")
        if rule.get("stderr"):
            sys.stderr.write(rule["stderr"] + "\n")
        sys.exit(rule.get("exit", 0))

sys.stderr.write(f"shim: unmatched call: {cmd} {joined}\n")
sys.exit(97)
