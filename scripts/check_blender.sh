#!/bin/sh
# Optional Blender lane (not part of CI): the offline Blender adapter checks against a real Blender.
#
#   SKID_BLENDER=/path/to/blender sh scripts/check_blender.sh
#
# Runs, from the repository root: the in-Blender smoke test (tests/blender_smoke.py; it writes
# previews under artifacts/blender-preview/) and the Python integration tests that need a real
# Blender (tests/test_integration.py with SKID_BLENDER set). The verified local runtime was
# Blender 5.1.1; other versions are untested. On macOS, background Blender may need to run
# outside restrictive sandboxes. Exit status is nonzero when any check fails.
set -eu
if [ -z "${SKID_BLENDER:-}" ] || [ ! -x "$SKID_BLENDER" ]; then
  echo "error: set SKID_BLENDER to an installed Blender executable (nothing is downloaded)" >&2
  exit 2
fi
cd "$(dirname "$0")/.."
PYTHON="${SKID_PYTHON:-python3}"
# Capture the version query first (POSIX sh has no pipefail), so a failing --version stops the lane.
version=$("$SKID_BLENDER" --version) || {
  status=$?
  echo "error: $SKID_BLENDER --version failed (exit $status)" >&2
  exit "$status"
}
printf '%s\n' "$version" | head -n 1
"$SKID_BLENDER" --background --factory-startup --python-exit-code 1 --python tests/blender_smoke.py
SKID_BLENDER="$SKID_BLENDER" "$PYTHON" -m unittest tests.test_integration -v
