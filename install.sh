#!/usr/bin/env bash
set -euo pipefail

function installer_usage {
    cat <<'USAGE'
Usage: ./install.sh [--client | --server] [options]

  --client          Install sidebar config and font on this UI computer (default).
  --server          Install/link the plugin on the machine running the agents.
  --dry-run         Show the config diff without writing files or starting services.
  --replace-radar   Replace an existing Radar-managed sidebar block.
  --config PATH     Use an explicit Herdr config file.
  --display-mode MODE  Server mode: quota (default), auto, estimated, or billed.
  --rollback PATH   Restore a backup directory printed by this installer.
  --help            Show this help.

Run from a complete checkout. Requires Bash and Python 3.11+.
Set PYTHON_BIN to select an interpreter. Client mode never connects to a server.
After installation, use Herdr's global menu -> reload config on the UI computer.
USAGE
}

INSTALLER_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER_MODE='client'
INSTALLER_MODE_SET=false
INSTALLER_APPLY=true
INSTALLER_ROLLBACK=false
INSTALLER_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --client|--server)
            if [[ "$INSTALLER_MODE_SET" == true && "$INSTALLER_MODE" != "${1#--}" ]]; then
                printf '%s\n' 'Choose either --client or --server.' >&2
                exit 2
            fi
            INSTALLER_MODE="${1#--}"
            INSTALLER_MODE_SET=true
            shift
            ;;
        --dry-run) INSTALLER_APPLY=false; shift ;;
        --replace-radar) INSTALLER_ARGS+=("$1"); shift ;;
        --config|--rollback|--display-mode)
            if [[ $# -lt 2 || -z "$2" || "$2" == --* ]]; then
                printf '%s requires a value.\n' "$1" >&2
                exit 2
            fi
            INSTALLER_ARGS+=("$1" "$2")
            if [[ "$1" == --rollback ]]; then INSTALLER_ROLLBACK=true; fi
            shift 2
            ;;
        --help) installer_usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; installer_usage >&2; exit 2 ;;
    esac
done

if [[ "$INSTALLER_ROLLBACK" == true && "$INSTALLER_APPLY" == false ]]; then
    printf '%s\n' '--dry-run cannot be combined with --rollback; no changes made.' >&2
    exit 2
fi

if [[ -n "${PYTHON_BIN:-}" ]]; then
    INSTALLER_PYTHONS=("$PYTHON_BIN")
else
    INSTALLER_PYTHONS=(python3 python3.14 python3.13 python3.12 python3.11)
fi
INSTALLER_PYTHON=''
for PYTHON_CANDIDATE in "${INSTALLER_PYTHONS[@]}"; do
    if command -v "$PYTHON_CANDIDATE" >/dev/null 2>&1 &&
        "$PYTHON_CANDIDATE" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        INSTALLER_PYTHON="$PYTHON_CANDIDATE"
        break
    fi
done
if [[ -z "$INSTALLER_PYTHON" ]]; then
    printf '%s\n' 'Python 3.11+ is required; install it or set PYTHON_BIN to its executable.' >&2
    exit 1
fi

if [[ "$INSTALLER_MODE" == client ]]; then INSTALLER_ARGS+=(--client); fi
if [[ "$INSTALLER_APPLY" == true ]]; then INSTALLER_ARGS+=(--apply); fi
exec "$INSTALLER_PYTHON" -B "$INSTALLER_ROOT/install.py" "${INSTALLER_ARGS[@]}"
