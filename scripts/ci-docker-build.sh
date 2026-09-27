#!/usr/bin/env bash
# CI wrapper for `docker build`: bounded time, and a readable failure.
#
# Usage: scripts/ci-docker-build.sh NAME -- <docker build arguments>
#
# The full BuildKit log goes to a file on the runner, never to the job log.
# On failure or timeout only the last build-step headers are printed; those are
# the Dockerfile instructions, which live in Git and carry no build output.
set -uo pipefail

name=${1:?build name required}
shift
[ "${1:-}" = "--" ] && shift
case "$name" in *[!A-Za-z0-9._-]*) echo "::error::invalid build name" >&2; exit 2 ;; esac

log="${RUNNER_TEMP:-/tmp}/docker-build-${name}.log"
limit="${CI_DOCKER_BUILD_TIMEOUT:-900}"

timeout --kill-after=30 "$limit" docker build --progress=plain "$@" >"$log" 2>&1
status=$?
if [ "$status" -ne 0 ]; then
  if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
    echo "::error::docker build '$name' timed out after ${limit}s"
  else
    echo "::error::docker build '$name' failed (exit $status)"
  fi
  echo "Last build steps started (Dockerfile instructions only):"
  grep -E '^#[0-9]+ \[[^]]+\] ' "$log" | sed -E 's/^#[0-9]+ //' | awk '!seen[$0]++' | tail -n 12 | cut -c1-200
  exit "$status"
fi
