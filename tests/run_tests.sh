#!/usr/bin/env bash
# Run Render Blame's checks in headless Blender.
set -euo pipefail
cd "$(dirname "$0")/.."
B="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
[[ -f tests/scene.blend ]] || "$B" -b --factory-startup --python tests/make_scene.py >/dev/null
"$B" -b tests/scene.blend --factory-startup --python-exit-code 1 --python tests/test_e2e.py 2>&1 | grep -E "^(PASS|FAIL|ALL|[0-9]+ failed)|Error"
