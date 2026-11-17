#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${TMPDIR:=/data/tmp}"
: "${PGPASSFILE:=/tmp/pgpass}"
export TMPDIR PGPASSFILE

mkdir -p "$TMPDIR"
if [[ ! -w "$TMPDIR" ]]; then
    echo "ERROR: scratch directory is not writable: $TMPDIR" >&2
    exit 1
fi

if [[ -n "${PGPASS_SOURCE:-}" ]]; then
    if [[ ! -f "$PGPASS_SOURCE" || ! -r "$PGPASS_SOURCE" ]]; then
        echo "ERROR: requested credential file is missing or unreadable: $PGPASS_SOURCE" >&2
        exit 1
    fi
    install -m 0600 "$PGPASS_SOURCE" "$PGPASSFILE"
fi

exec "$@"
