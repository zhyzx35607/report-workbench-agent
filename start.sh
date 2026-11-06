#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
if ! .venv/bin/python -c 'import fastapi, uvicorn, report_workbench, multipart, markdown_it, httpx' 2>/dev/null; then
  .venv/bin/python -m pip install -e .
fi
exec .venv/bin/python -m uvicorn report_workbench.app:app --host 127.0.0.1 --port 8767
