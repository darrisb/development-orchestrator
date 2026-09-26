#!/usr/bin/env bash
# Developer entrypoints. Run from the repository root.
set -euo pipefail

VENV="${VENV:-.venv}"

case "${1:-help}" in
  setup)
    uv venv --python 3.12 "$VENV"
    uv pip install --python "$VENV/bin/python" -e ".[dev]"
    ;;
  test)    "$VENV/bin/python" -m pytest "${@:2}" ;;
  lint)    "$VENV/bin/ruff" check . "${@:2}" ;;
  migrate) "$VENV/bin/alembic" upgrade head ;;
  revision)
    [ -n "${2:-}" ] || { echo "usage: dev.sh revision <message>" >&2; exit 2; }
    "$VENV/bin/alembic" revision --autogenerate -m "$2"
    ;;
  workers)
    # Build the disposable worker images (build.md section 11). Tags must match
    # WORKER_*_IMAGE in the environment; the defaults are these.
    profiles=("${@:2}")
    [ "${#profiles[@]}" -gt 0 ] || profiles=(node python java)
    for profile in "${profiles[@]}"; do
      docker build -t "orchestrator-worker-$profile:latest" "workers/$profile"
    done
    ;;
  serve)
    "$VENV/bin/uvicorn" apps.orchestrator.main:app --reload --port "${PORT:-8000}"
    ;;
  *)
    echo "usage: dev.sh {setup|test|lint|migrate|revision <msg>|workers [profile...]|serve}" >&2
    exit 2
    ;;
esac
