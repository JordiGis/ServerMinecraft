#!/usr/bin/env bash
#
# setup.sh — configurador interactivo del servidor Minecraft (Docker).
# Genera un fichero .env y opcionalmente arranca el servidor.
#
set -euo pipefail

cd "$(dirname "$0")"

ENV_FILE=".env"

# ---- helpers de UI ---------------------------------------------------------

c_reset=$'\033[0m'; c_bold=$'\033[1m'; c_cyan=$'\033[36m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'

title() { printf '\n%s%s== %s ==%s\n' "$c_bold" "$c_cyan" "$1" "$c_reset"; }

# menu "Pregunta" default opt1 opt2 ...
# devuelve la opcion elegida por stdout
menu() {
  local prompt="$1" default="$2"; shift 2
  local opts=("$@") i choice
  {
    echo
    echo "$prompt"
    for i in "${!opts[@]}"; do
      printf '  %s%d%s) %s\n' "$c_green" "$((i+1))" "$c_reset" "${opts[$i]}"
    done
    printf '  Elige [%s]: ' "$default"
  } >&2
  read -r choice || true
  choice="${choice:-$default}"
  if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#opts[@]} )); then
    echo "${opts[$((choice-1))]}"
  else
    echo "${opts[$((default-1))]}"
  fi
}

# ask "Pregunta" "valor_por_defecto"
ask() {
  local prompt="$1" def="$2" ans
  printf '%s [%s]: ' "$prompt" "$def" >&2
  read -r ans || true
  echo "${ans:-$def}"
}

# ---- inicio ----------------------------------------------------------------

printf '%s%s\n' "$c_bold" "$c_cyan"
cat <<'BANNER'
  __  __ _                            __ _
 |  \/  (_)_ __   ___  ___ _ __ __ _ / _| |_
 | |\/| | | '_ \ / _ \/ __| '__/ _` | |_| __|
 | |  | | | | | |  __/ (__| | | (_| |  _| |_
 |_|  |_|_|_| |_|\___|\___|_|  \__,_|_|  \__|
        configurador Docker
BANNER
printf '%s' "$c_reset"

# 1) Tipo de servidor
TYPE=$(menu "Tipo de servidor:" 1 \
  "PAPER" "VANILLA" "FABRIC" "FORGE" "SPIGOT" "PURPUR")

# 2) Version
VERSION=$(menu "Version de Minecraft:" 1 \
  "LATEST" "1.21" "1.20.6" "1.20.4" "1.19.4" "otra")
if [[ "$VERSION" == "otra" ]]; then
  VERSION=$(ask "Escribe la version (ej. 1.21.1)" "LATEST")
fi

# 3) Modo online
mode=$(menu "Modo del servidor:" 1 \
  "Online (premium, solo cuentas compradas)" \
  "Offline (cracked, permite no premium)")
[[ "$mode" == Online* ]] && ONLINE_MODE=TRUE || ONLINE_MODE=FALSE

# 4) Dificultad
DIFFICULTY=$(menu "Dificultad:" 2 "peaceful" "normal" "easy" "hard")

# 5) Ajustes libres
SERVER_NAME=$(ask "Nombre del contenedor" "minecraft")
MEMORY=$(ask "Memoria RAM (ej. 2G, 4G)" "2G")
PORT=$(ask "Puerto del host" "25565")
MAX_PLAYERS=$(ask "Maximo de jugadores" "20")
MOTD=$(ask "MOTD (mensaje del servidor)" "Servidor Minecraft Docker")
OPS=$(ask "Operadores/admins (nicks separados por coma, vacio = ninguno)" "")
TZ=$(ask "Zona horaria" "Europe/Madrid")

# ---- escribir .env ---------------------------------------------------------

if [[ -f "$ENV_FILE" ]]; then
  cp "$ENV_FILE" "${ENV_FILE}.bak"
  printf '\n%sAviso:%s .env existente respaldado en %s.bak\n' "$c_yellow" "$c_reset" "$ENV_FILE"
fi

cat > "$ENV_FILE" <<EOF
# Generado por setup.sh
SERVER_NAME=$SERVER_NAME
TYPE=$TYPE
VERSION=$VERSION
ONLINE_MODE=$ONLINE_MODE
MEMORY=$MEMORY
PORT=$PORT
DIFFICULTY=$DIFFICULTY
MAX_PLAYERS=$MAX_PLAYERS
MOTD=$MOTD
OPS=$OPS
TZ=$TZ
EOF

title "Configuracion guardada en $ENV_FILE"
cat "$ENV_FILE"

if [[ "$ONLINE_MODE" == "FALSE" ]]; then
  printf '\n%sSeguridad:%s modo OFFLINE activo. Cualquiera puede entrar con cualquier nick; no hay autenticacion de Mojang. Usa whitelist/OPS y no lo expongas a internet sin proteccion.\n' "$c_yellow" "$c_reset"
fi

# ---- arrancar --------------------------------------------------------------

start=$(menu "Arrancar el servidor ahora?" 1 "Si" "No")
if [[ "$start" == "Si" ]]; then
  title "Arrancando (docker compose up -d)"
  docker compose up -d
  echo
  echo "Logs en vivo:   docker compose logs -f"
  echo "Consola RCON:   docker exec -i $SERVER_NAME rcon-cli"
  echo "Parar:          docker compose down"
else
  echo
  echo "Para arrancar manualmente: docker compose up -d"
fi
