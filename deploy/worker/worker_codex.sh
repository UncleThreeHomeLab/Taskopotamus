#!/bin/sh
set -eu
cd /workspace
exec /usr/local/lib/python3.13/site-packages/codex_cli_bin/bin/codex "$@"
