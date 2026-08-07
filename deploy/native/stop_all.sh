#!/usr/bin/env bash
set -uo pipefail
HERE="$(dirname "${BASH_SOURCE[0]}")"
bash "$HERE/stop_ollama.sh" || true
bash "$HERE/stop_redis.sh"  || true
bash "$HERE/stop_pg.sh"     || true
