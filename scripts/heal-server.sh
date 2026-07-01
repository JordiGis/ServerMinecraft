#!/usr/bin/env bash
#
# heal-server.sh — arranca el servidor en bucle y saca a cuarentena (auto)
# los mods que impiden el arranque en un server DEDICADO:
#   - mods client-only que crashean con "invalid dist DEDICATED_SERVER"
#
# Los mods no se borran: se mueven a data/client-only-removed/ (reversible).
# Uso:  ./scripts/heal-server.sh [max_intentos]
#
set -euo pipefail
cd "$(dirname "$0")/.."

CONTAINER="${SERVER_NAME:-}"
[ -z "$CONTAINER" ] && CONTAINER="$(grep -E '^SERVER_NAME=' .env 2>/dev/null | cut -d= -f2)"
CONTAINER="${CONTAINER:-minecraft}"

MODS_DIR="data/mods"
QUAR_DIR="data/client-only-removed"
MAX="${1:-15}"
mkdir -p "$QUAR_DIR"

# Devuelve el jar (ruta) que declara el modId dado, buscando en su mods.toml.
jar_for_modid() {
  local modid="$1" j toml
  for j in "$MODS_DIR"/*.jar; do
    [ -e "$j" ] || continue
    toml="$(unzip -p "$j" META-INF/neoforge.mods.toml 2>/dev/null)"
    [ -z "$toml" ] && toml="$(unzip -p "$j" META-INF/mods.toml 2>/dev/null)"
    if printf '%s' "$toml" | grep -qE "modId[[:space:]]*=[[:space:]]*\"${modid}\""; then
      echo "$j"; return 0
    fi
  done
  return 1
}

wait_health() {
  local s
  while true; do
    s="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CONTAINER" 2>/dev/null || echo missing)"
    case "$s" in
      healthy|running) echo "$s"; return 0 ;;
      unhealthy|exited|dead|missing) echo "$s"; return 1 ;;
    esac
    sleep 5
  done
}

echo "== heal-server: contenedor '$CONTAINER', hasta $MAX intentos =="
for i in $(seq 1 "$MAX"); do
  echo "--- intento $i: reiniciando ---"
  docker restart "$CONTAINER" >/dev/null 2>&1 || docker compose up -d mc >/dev/null 2>&1
  if wait_health >/dev/null; then
    echo "OK: servidor arrancado (healthy)."
    exit 0
  fi

  # Buscar el modId que crashea por dist cliente en server.
  logs="$(docker logs --tail 200 "$CONTAINER" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')"
  modid="$(echo "$logs" | grep -oE '\(([a-z0-9_]+)\) has failed to load' | grep -oE '\([a-z0-9_]+\)' | tr -d '()' | tail -1)"

  if [ -z "$modid" ]; then
    # Fallback: cualquier "invalid dist" no siempre nombra el modid limpio.
    echo "No se pudo identificar el mod culpable automaticamente."
    echo "Ultimo error relevante:"
    echo "$logs" | grep -iE 'invalid dist|has failed to load|Failed to start' | tail -5
    exit 1
  fi

  jar="$(jar_for_modid "$modid" || true)"
  if [ -z "$jar" ]; then
    echo "Mod '$modid' crashea pero no encuentro su .jar. Aborto."
    exit 1
  fi
  echo "Cuarentena: $modid -> $(basename "$jar")"
  mv "$jar" "$QUAR_DIR/"
done

echo "Alcanzado el maximo de intentos ($MAX) sin arrancar."
exit 1
