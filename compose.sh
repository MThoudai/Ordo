#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$PROJECT_DIR/backend/.env" ]]; then
  echo "Missing backend/.env. Configure the model service before starting." >&2
  exit 1
fi
if [[ $# -eq 0 ]]; then set -- ps; fi
exec docker compose --project-name ordo --project-directory "$PROJECT_DIR" \
  --env-file "$PROJECT_DIR/backend/.env" -f "$PROJECT_DIR/docker-compose.yml" "$@"
