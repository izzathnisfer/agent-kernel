#!/bin/bash

set -euo pipefail
if command -v pyenv >/dev/null 2>&1; then
  uv venv --python "$(pyenv which python)" --allow-existing
else
  uv venv --allow-existing
fi

if [[ ${1-} != "local" ]]; then
  uv sync --all-extras
else
  uv sync --find-links ../../../ak-py/dist --all-extras
  dist_dir="$(cd ../../../ak-py/dist && pwd)"
  wheel="$(ls -t "$dist_dir"/agentkernel-*.whl 2>/dev/null | head -1 || true)"
  if [[ -z "$wheel" ]]; then
    echo "No agentkernel wheel in $dist_dir — build it first: (cd ../../../ak-py && uv build --wheel)" >&2
    exit 1
  fi
  uv pip install --python .venv/bin/python --refresh-package agentkernel --reinstall-package agentkernel \
    "agentkernel[cli,pydanticai,logfire,test] @ file://${wheel}"
fi
