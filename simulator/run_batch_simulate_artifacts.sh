#!/usr/bin/env bash
# Convenience wrapper for batch_simulate_artifacts.py
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OUT_PATH="${SCRIPT_DIR}/simulator_batch_results.json"
ARGS=("$@")

# Track --output so we summarize the same JSON file the simulator writes.
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  if [[ "${ARGS[$i]}" == "--output" ]] && ((i + 1 < ${#ARGS[@]})); then
    OUT_PATH="${ARGS[$((i + 1))]}"
  elif [[ "${ARGS[$i]}" == --output=* ]]; then
    OUT_PATH="${ARGS[$i]#--output=}"
  fi
done

python3 "${SCRIPT_DIR}/batch_simulate_artifacts.py" "${ARGS[@]}"

python3 - "$OUT_PATH" <<'PY'
import json
import sys
from pathlib import Path

out_path = Path(sys.argv[1]).expanduser().resolve()
payload = json.loads(out_path.read_text(encoding="utf-8"))
tests = payload.get("tests", [])

speed_sum = 0.0
mem_sum = 0.0
count = 0

for test in tests:
    if test.get("simulation_failed") is True:
        continue

    saved_speed = test.get("saved_speed")
    saved_memory = test.get("saved_memory")
    if saved_speed is None or saved_memory is None:
        continue

    speed = float(str(saved_speed).replace("+", ""))
    memory = float(str(saved_memory).replace("+", ""))
    if speed == 0.0 or memory == 0.0:
        continue

    speed_sum += speed
    mem_sum += memory
    count += 1

if count == 0:
    print(f"Summary ({out_path}): no eligible tests matched filters.")
else:
    print(f"Summary ({out_path}):")
    print(f"  Eligible tests: {count}")
    print(f"  Average saved_speed: {speed_sum / count:.6f}")
    print(f"  Average saved_memory: {mem_sum / count:.6f}")
PY
