#!/usr/bin/env zsh
# setup-venv.zsh — create the Python environment the scripts use (tools/render.py).
#   scripts/setup-venv.zsh            runtime deps into .venv
#   scripts/setup-venv.zsh --tests    plus pytest
setopt errexit nounset pipefail
root="${0:A:h:h}"
py="${PYTHON:-python3}"
command -v "$py" >/dev/null || { print -u2 "python3 not found"; exit 1; }
[[ -x "$root/.venv/bin/python" ]] || "$py" -m venv "$root/.venv"
"$root/.venv/bin/python" -m pip install --quiet --upgrade pip
if [[ "${1:-}" == --tests ]]; then
  "$root/.venv/bin/python" -m pip install --quiet -r "$root/tests/requirements.txt"
else
  "$root/.venv/bin/python" -m pip install --quiet -r "$root/tools/requirements.txt"
fi
print "venv ready: $root/.venv"
