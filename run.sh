#!/usr/bin/env bash
# Lanza DictaVoz desde su carpeta, sin instalar nada.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
exec python3 -m dictavoz "$@"
