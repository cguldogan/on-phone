#!/bin/zsh
# Create .env from .env.example with fresh random secrets. Refuses to overwrite an existing .env.
set -euo pipefail
ROOT=${0:A:h:h}
[[ -e $ROOT/.env ]] && { echo ".env already exists; not overwriting" >&2; exit 1; }
gen() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c $1; }
sed -E 's/[[:space:]]+#.*$//' $ROOT/.env.example | while IFS= read -r line; do
  case $line in
    ARI_PASSWORD=*) print -r -- "ARI_PASSWORD=$(gen 32)" ;;
    MCP_BEARER_TOKEN=*) print -r -- "MCP_BEARER_TOKEN=$(gen 40)" ;;
    TEST_EXT_PASSWORD=*) print -r -- "TEST_EXT_PASSWORD=$(gen 20)" ;;
    *) print -r -- "$line" ;;
  esac
done > $ROOT/.env
chmod 600 $ROOT/.env
echo "wrote $ROOT/.env"
