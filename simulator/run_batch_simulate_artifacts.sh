#!/usr/bin/env bash
# Convenience wrapper for batch_simulate_artifacts.py
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/batch_simulate_artifacts.py" "$@"
