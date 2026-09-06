#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"

# A local .env is optional and ignored by Git. On a scheduled host, keep it
# readable only by the service account (for example: chmod 600 .env).
if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi

: "${BELLHAVEN_API_TOKEN:?Set BELLHAVEN_API_TOKEN in the scheduler environment or .env}"

python3 bellhaven_sync.py run
python3 monitor.py
