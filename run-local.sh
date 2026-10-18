#!/usr/bin/env bash
# Local testing harness for Huberry Stream (webserver-videoplayer)
# Starts:
#   1. mpv IPC server listening on /tmp/mpv-socket (windowed if DISPLAY/WAYLAND_DISPLAY is active, else headless)
#   2. Flask web server (python-remote/remote.py) on http://localhost:5000
#
# Usage:
#   ./run-local.sh              # Run interactively on port 5000
#   ./run-local.sh --headless   # Force headless mpv (--vo=null --ao=null)
#   ./run-local.sh --stop       # Stop running local Flask & mpv instances

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_DIR="${REPO_DIR}/python-remote"
VENV_PYTHON="${REMOTE_DIR}/venv/bin/python"
MPV_SOCKET="${MPV_IPC_PATH:-/tmp/mpv-socket}"
PORT="${FLASK_PORT:-5000}"
MOVIES_PATH="${MOVIES_DIR:-${HOME}/movies}"

if [[ "${1:-}" == "--stop" ]]; then
    echo "Stopping local Huberry Stream (remote.py) and mpv on ${MPV_SOCKET}..."
    pkill -f "python.*remote\.py" 2>/dev/null || true
    pkill -f "input-ipc-server=${MPV_SOCKET}" 2>/dev/null || true
    rm -f "${MPV_SOCKET}"
    echo "Stopped."
    exit 0
fi

FORCE_HEADLESS=0
if [[ "${1:-}" == "--headless" ]]; then
    FORCE_HEADLESS=1
fi

if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "Error: Python virtualenv not found at ${VENV_PYTHON}"
    echo "Run: python3 -m venv ${REMOTE_DIR}/venv && ${REMOTE_DIR}/venv/bin/pip install flask requests"
    exit 1
fi

mkdir -p "${MOVIES_PATH}"

# Start or reuse mpv listening on MPV_SOCKET
if ! pgrep -f "input-ipc-server=${MPV_SOCKET}" >/dev/null 2>&1; then
    rm -f "${MPV_SOCKET}"
    if [[ "${FORCE_HEADLESS}" -eq 1 ]] || [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
        echo "[1/2] Starting mpv (headless IPC mode) on ${MPV_SOCKET}..."
        mpv --idle=yes --vo=null --ao=null --keep-open=yes \
            --sub-codepage=utf-8:cp1250 \
            "--input-ipc-server=${MPV_SOCKET}" >/dev/null 2>&1 &
    else
        echo "[1/2] Starting mpv (windowed 4K/HDR tone-mapped mode) on ${MPV_SOCKET}..."
        mpv --idle=yes --force-window=immediate --keep-open=yes \
            --cache=yes --demuxer-max-bytes=128MiB --demuxer-max-back-bytes=32MiB \
            --hdr-compute-peak=no --tone-mapping=bt.2390 \
            --audio-channels=auto-safe --ad-lavc-downmix=yes \
            --sub-codepage=utf-8:cp1250 \
            "--input-ipc-server=${MPV_SOCKET}" >/dev/null 2>&1 &
    fi
    for _ in {1..25}; do
        [[ -S "${MPV_SOCKET}" ]] && break
        sleep 0.1
    done
else
    echo "[1/2] Reusing active mpv IPC socket at ${MPV_SOCKET}"
fi

echo "[2/2] Starting Huberry Stream server on http://localhost:${PORT}"
echo "      Movies directory : ${MOVIES_PATH}"
echo "      mpv IPC socket   : ${MPV_SOCKET}"
echo "      Stop anytime with: ./run-local.sh --stop"
echo ""

cd "${REMOTE_DIR}"
exec env MOVIES_DIR="${MOVIES_PATH}" MPV_IPC_PATH="${MPV_SOCKET}" FLASK_PORT="${PORT}" \
    "${VENV_PYTHON}" remote.py
