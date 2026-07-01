# Servidor Minecraft en Docker

Servidor Minecraft configurable mediante un script interactivo. Usa la imagen
[`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server).

## Requisitos

- Docker + Docker Compose
- Docker Desktop **abierto** (daemon en marcha)

## Uso rapido

```bash
./setup.sh
```

El menú te pregunta:

| Opción       | Qué configura                                      |
|--------------|----------------------------------------------------|
| Tipo         | PAPER / VANILLA / FABRIC / FORGE / SPIGOT / PURPUR |
| Versión      | LATEST o una versión concreta (ej. 1.21.1)         |
| Modo         | Online (premium) / Offline (cracked)               |
| Dificultad   | peaceful / easy / normal / hard                    |
| RAM, puerto, nombre, jugadores, MOTD, OPS, TZ      |                     |

Genera un `.env` y (opcional) arranca el servidor.

## Comandos

```bash
docker compose up -d          # arrancar
docker compose logs -f        # ver logs
docker compose down           # parar
docker exec -i minecraft rcon-cli   # consola del servidor
```

## Datos

El mundo y config viven en `./data` (mapeado al volumen). No se sube a git.

## Reconfigurar

Vuelve a ejecutar `./setup.sh` (respalda el `.env` anterior en `.env.bak`) y
luego `docker compose up -d`.
