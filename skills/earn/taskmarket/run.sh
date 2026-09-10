#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd -P)"
exec node "$HERE/taskmarket-work.mjs"
