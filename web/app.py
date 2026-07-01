"""Panel web ligero para monitorizar el servidor Minecraft (itzg).

Funciones:
  - Estado del contenedor + jugadores online (via RCON `list`)
  - Consola RCON (ejecutar comandos)
  - Metricas CPU/RAM del contenedor (Docker API)
  - Control start/stop/restart (Docker API)
"""
import os
import datetime
import functools
import re
import io
import json
import shutil
import socket
import struct
import tempfile
import threading
import time
import urllib.request
import urllib.parse
import zipfile
from pathlib import Path

from flask import Flask, request, jsonify, Response, render_template, send_file
from werkzeug.utils import secure_filename
import docker

RCON_HOST = os.environ.get("RCON_HOST", "mc")
RCON_PORT = int(os.environ.get("RCON_PORT", "25575"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "minecraft")
MC_CONTAINER = os.environ.get("MC_CONTAINER", "minecraft")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MODS_DIR = Path(os.environ.get("MODS_DIR", "/data/mods"))
CF_API_KEY = os.environ.get("CF_API_KEY", "")
BACKUP_DIR = DATA_DIR / "backups"
SCHEDULE_FILE = DATA_DIR / "panel-schedule.json"

app = Flask(__name__)
_docker = docker.from_env()


# ---------------------------------------------------------------------------
# RCON (protocolo Source RCON, implementacion minima)
# ---------------------------------------------------------------------------
class RconError(Exception):
    pass


class Rcon:
    def __init__(self, host, port, password, timeout=5):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.sock = None
        self._id = 0

    def __enter__(self):
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock.settimeout(self.timeout)
        if self._send(3, self.password) is None:
            raise RconError("autenticacion RCON fallida")
        return self

    def __exit__(self, *a):
        if self.sock:
            self.sock.close()

    def _send(self, ptype, body):
        self._id += 1
        req_id = self._id
        payload = struct.pack("<ii", req_id, ptype) + body.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(payload)) + payload)
        resp_id, data = self._recv()
        if ptype == 3 and resp_id == -1:
            return None  # auth fail
        return data

    def _recv(self):
        length = struct.unpack("<i", self._read(4))[0]
        raw = self._read(length)
        resp_id, _ptype = struct.unpack("<ii", raw[:8])
        body = raw[8:-2].decode("utf-8", errors="replace")
        return resp_id, body

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("conexion RCON cerrada")
            buf += chunk
        return buf

    def command(self, cmd):
        return self._send(2, cmd)


def rcon_command(cmd):
    with Rcon(RCON_HOST, RCON_PORT, RCON_PASSWORD) as r:
        return r.command(cmd)


# ---------------------------------------------------------------------------
# Auth opcional (HTTP Basic si PANEL_PASSWORD esta definido)
# ---------------------------------------------------------------------------
def require_auth(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if PANEL_PASSWORD:
            auth = request.authorization
            if not auth or auth.password != PANEL_PASSWORD:
                return Response(
                    "Auth requerida", 401,
                    {"WWW-Authenticate": 'Basic realm="Panel Minecraft"'},
                )
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Helpers Docker
# ---------------------------------------------------------------------------
def get_container():
    return _docker.containers.get(MC_CONTAINER)


def container_env(c):
    return dict(
        e.split("=", 1) for e in c.attrs["Config"]["Env"] if "=" in e
    )


def cpu_mem_stats(c):
    s = c.stats(stream=False)
    # CPU %
    cpu_pct = 0.0
    try:
        cpu = s["cpu_stats"]
        pre = s["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu["system_cpu_usage"] - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1])
        if sys_delta > 0 and cpu_delta > 0:
            cpu_pct = (cpu_delta / sys_delta) * ncpu * 100.0
    except (KeyError, TypeError):
        pass
    # Mem
    mem = s.get("memory_stats", {})
    used = mem.get("usage", 0) - mem.get("stats", {}).get("inactive_file", 0)
    limit = mem.get("limit", 0)
    return {
        "cpu_pct": round(cpu_pct, 1),
        "mem_used_mb": round(used / 1024 / 1024, 1),
        "mem_limit_mb": round(limit / 1024 / 1024, 1),
        "mem_pct": round(used / limit * 100, 1) if limit else 0,
    }


def parse_players(list_output):
    # "There are 2 of a max of 20 players online: alice, bob"
    if not list_output:
        return {"online": 0, "max": 0, "names": []}
    try:
        head, _, tail = list_output.partition(":")
        parts = head.split()
        online = int(parts[2])
        maximum = int(parts[7])
        names = [n.strip() for n in tail.split(",") if n.strip()]
        return {"online": online, "max": maximum, "names": names}
    except (IndexError, ValueError):
        return {"online": 0, "max": 0, "names": [], "raw": list_output}


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------
@app.route("/")
@require_auth
def index():
    return render_template("index.html", container=MC_CONTAINER)


@app.route("/api/status")
@require_auth
def api_status():
    try:
        c = get_container()
    except docker.errors.NotFound:
        return jsonify({"state": "missing", "container": MC_CONTAINER})

    env = container_env(c)
    out = {
        "container": MC_CONTAINER,
        "state": c.status,
        "started_at": c.attrs["State"].get("StartedAt"),
        "health": c.attrs["State"].get("Health", {}).get("Status"),
        "type": env.get("TYPE"),
        "version": env.get("VERSION"),
        "port": env.get("SERVER_PORT", "25565"),
    }
    if c.status == "running":
        try:
            out["stats"] = cpu_mem_stats(c)
        except Exception as e:  # noqa: BLE001
            out["stats_error"] = str(e)
        try:
            out["players"] = parse_players(rcon_command("list"))
        except Exception as e:  # noqa: BLE001
            out["players_error"] = str(e)
    return jsonify(out)


@app.route("/api/command", methods=["POST"])
@require_auth
def api_command():
    cmd = (request.json or {}).get("command", "").strip()
    if not cmd:
        return jsonify({"error": "comando vacio"}), 400
    try:
        return jsonify({"output": rcon_command(cmd)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502


@app.route("/api/power", methods=["POST"])
@require_auth
def api_power():
    action = (request.json or {}).get("action")
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "accion invalida"}), 400
    try:
        c = get_container()
        getattr(c, action)()
        return jsonify({"ok": True, "action": action})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------------------
# Mods
# ---------------------------------------------------------------------------
def _safe_mod_name(name):
    name = secure_filename(name or "")
    if not name.lower().endswith(".jar"):
        raise ValueError("solo se permiten ficheros .jar")
    return name


@app.route("/api/mods")
@require_auth
def api_mods_list():
    MODS_DIR.mkdir(parents=True, exist_ok=True)
    mods = []
    for f in sorted(MODS_DIR.iterdir()):
        low = f.name.lower()
        if not f.is_file():
            continue
        if low.endswith(".jar"):
            mods.append({"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1),
                         "enabled": True})
        elif low.endswith(".jar.disabled"):
            mods.append({"name": f.name[:-len(".disabled")],
                         "size_kb": round(f.stat().st_size / 1024, 1), "enabled": False})
    mods.sort(key=lambda m: m["name"].lower())
    return jsonify({"mods": mods, "dir": str(MODS_DIR)})


@app.route("/api/mods/<name>/toggle", methods=["POST"])
@require_auth
def api_mods_toggle(name):
    try:
        name = _safe_mod_name(name)  # nombre base .jar
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    enabled = MODS_DIR / name
    disabled = MODS_DIR / (name + ".disabled")
    if enabled.is_file():
        enabled.rename(disabled)
        return jsonify({"ok": True, "enabled": False, "name": name})
    if disabled.is_file():
        disabled.rename(enabled)
        return jsonify({"ok": True, "enabled": True, "name": name})
    return jsonify({"error": "no existe"}), 404


@app.route("/api/mods", methods=["POST"])
@require_auth
def api_mods_upload():
    MODS_DIR.mkdir(parents=True, exist_ok=True)

    # Subida de fichero (multipart)
    if "file" in request.files:
        f = request.files["file"]
        try:
            name = _safe_mod_name(f.filename)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        f.save(MODS_DIR / name)
        return jsonify({"ok": True, "name": name})

    # Descarga desde URL
    url = (request.json or {}).get("url", "").strip() if request.is_json else ""
    if url:
        if not url.startswith(("http://", "https://")):
            return jsonify({"error": "URL invalida"}), 400
        name = url.split("?")[0].rstrip("/").split("/")[-1]
        try:
            name = _safe_mod_name(name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            (MODS_DIR / name).write_bytes(data)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"descarga fallida: {e}"}), 502
        return jsonify({"ok": True, "name": name, "size_kb": round(len(data) / 1024, 1)})

    return jsonify({"error": "falta fichero o url"}), 400


@app.route("/api/mods/<name>", methods=["DELETE"])
@require_auth
def api_mods_delete(name):
    try:
        name = _safe_mod_name(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    target = MODS_DIR / name
    if not target.is_file():
        return jsonify({"error": "no existe"}), 404
    target.unlink()
    return jsonify({"ok": True, "name": name})


# ---------------------------------------------------------------------------
# Modpacks (ZIP de CurseForge o server pack)
# ---------------------------------------------------------------------------
UA = "Mozilla/5.0 (compatible; mc-panel)"
LOADER_MAP = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}


def _cf_get(url):
    req = urllib.request.Request(
        url, headers={"x-api-key": CF_API_KEY, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["data"]


def _cf_file_url(file_data):
    """URL de descarga de un file-data de CurseForge (con fallback forgecdn)."""
    url = file_data.get("downloadUrl")
    fname = file_data.get("fileName")
    if not url and fname:
        fid = str(file_data["id"])
        enc = urllib.parse.quote(fname)  # nombre puede tener espacios/acentos
        url = f"https://edge.forgecdn.net/files/{fid[:4]}/{int(fid[4:])}/{enc}"
    return url, fname


def _cf_download(file_data):
    """Descarga un file-data a MODS_DIR. Devuelve el nombre o lanza excepcion."""
    url, fname = _cf_file_url(file_data)
    if not url:
        raise RconError("sin URL de descarga")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as resp:
        (MODS_DIR / secure_filename(fname)).write_bytes(resp.read())
    return fname


def _cf_loader_type(manifest):
    loaders = manifest.get("minecraft", {}).get("modLoaders", [])
    if loaders:
        name = loaders[0].get("id", "").split("-")[0].lower()
        return LOADER_MAP.get(name)
    return None


def _cf_latest_file(mod_id, game_version, loader_type):
    """Fichero mas reciente compatible de un mod (por version MC + loader)."""
    q = f"?gameVersion={urllib.parse.quote(game_version)}"
    if loader_type:
        q += f"&modLoaderType={loader_type}"
    data = _cf_get(f"https://api.curseforge.com/v1/mods/{mod_id}/files{q}")
    return data[0] if data else None  # la API los devuelve por fecha desc


def _required_deps(file_data):
    return [d["modId"] for d in file_data.get("dependencies", [])
            if d.get("relationType") == 3]  # 3 = RequiredDependency


def _resolve_deps(seed_files, game_version, loader_type, have, log):
    """Descarga recursivamente dependencias requeridas que falten."""
    from collections import deque
    queue = deque()
    for fd in seed_files:
        queue.extend(_required_deps(fd))
    added, visited = 0, set()
    while queue:
        mod_id = queue.popleft()
        if mod_id in have or mod_id in visited:
            continue
        visited.add(mod_id)
        try:
            fd = _cf_latest_file(mod_id, game_version, loader_type)
            if not fd:
                log.append(f"dep {mod_id}: sin fichero compatible")
                continue
            name = _cf_download(fd)
            have.add(mod_id)
            added += 1
            log.append(f"dep auto: {name}")
            queue.extend(_required_deps(fd))
        except Exception as e:  # noqa: BLE001
            log.append(f"dep {mod_id} fallo: {e}")
    return added


def _merge_tree(src: Path, dst: Path):
    """Copia recursivamente src dentro de dst, fusionando carpetas."""
    count = 0
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            count += 1
    return count


def _find_pack_root(extracted: Path):
    """Si el zip tiene una unica carpeta raiz envolvente, desciende a ella.
    No desciende si esa carpeta ya es parte del contenido del pack
    (mods/overrides/config), para no romper la deteccion de server pack."""
    entries = [p for p in extracted.iterdir() if not p.name.startswith("__MACOSX")]
    if (len(entries) == 1 and entries[0].is_dir()
            and entries[0].name.lower() not in ("mods", "overrides", "config")):
        return entries[0]
    return extracted


def _install_curseforge(root: Path, log):
    manifest = json.loads((root / "manifest.json").read_text())
    files = manifest.get("files", [])
    log.append(f"Modpack CurseForge: {manifest.get('name','?')} "
               f"({len(files)} mods, MC {manifest.get('minecraft',{}).get('version','?')})")
    loaders = manifest.get("minecraft", {}).get("modLoaders", [])
    if loaders:
        log.append(f"Loader recomendado: {loaders[0].get('id')}")

    if not CF_API_KEY:
        raise RconError(
            "Este ZIP es un export de CurseForge (solo IDs, sin los .jar). "
            "Configura CF_API_KEY en .env para descargarlos automaticamente, "
            "o exporta un 'server pack' con los jars incluidos."
        )

    MODS_DIR.mkdir(parents=True, exist_ok=True)
    game_version = manifest.get("minecraft", {}).get("version", "")
    loader_type = _cf_loader_type(manifest)

    installed, failed, seed = 0, [], []
    have = set(f.get("projectID") for f in files)
    for f in files:
        mod_id, file_id = f.get("projectID"), f.get("fileID")
        try:
            fd = _cf_get(f"https://api.curseforge.com/v1/mods/{mod_id}/files/{file_id}")
            _cf_download(fd)
            seed.append(fd)
            installed += 1
        except Exception as e:  # noqa: BLE001
            failed.append(f"{mod_id}/{file_id}: {e}")

    # Auto-resolucion de dependencias requeridas que falten (ej. Create)
    deps = _resolve_deps(seed, game_version, loader_type, have, log)

    # overrides -> /data
    ov = root / manifest.get("overrides", "overrides")
    if ov.is_dir():
        n = _merge_tree(ov, DATA_DIR)
        log.append(f"overrides copiados: {n} ficheros")

    log.append(f"Mods instalados: {installed}/{len(files)}" +
               (f" (+{deps} dependencias auto)" if deps else ""))
    if failed:
        log.append(f"Fallidos ({len(failed)}): " + "; ".join(failed[:10]))
    return {"installed": installed, "deps": deps, "failed": failed}


def _install_serverpack(root: Path, log):
    """ZIP con jars incluidos: fusiona todo (mods/, config/, etc.) en /data."""
    mods_dirs = [p for p in root.rglob("mods") if p.is_dir()]
    if not mods_dirs:
        raise RconError("ZIP no reconocido: sin manifest.json ni carpeta mods/")
    # Usa el directorio que contiene la carpeta mods/ como raiz del pack
    base = mods_dirs[0].parent
    n = _merge_tree(base, DATA_DIR)
    jars = len(list((DATA_DIR / "mods").glob("*.jar"))) if (DATA_DIR / "mods").is_dir() else 0
    log.append(f"Server pack: {n} ficheros copiados, {jars} mods en total")
    return {"copied": n, "mods_total": jars}


@app.route("/api/modpack", methods=["POST"])
@require_auth
def api_modpack():
    if "file" not in request.files:
        return jsonify({"error": "falta el fichero zip"}), 400
    f = request.files["file"]
    if not (f.filename or "").lower().endswith(".zip"):
        return jsonify({"error": "solo se permiten ficheros .zip"}), 400

    log = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with zipfile.ZipFile(io.BytesIO(f.read())) as z:
                z.extractall(tmp)
            root = _find_pack_root(tmp)
            if (root / "manifest.json").is_file():
                result = _install_curseforge(root, log)
            else:
                result = _install_serverpack(root, log)
        return jsonify({"ok": True, "log": log, "result": result})
    except RconError as e:
        return jsonify({"error": str(e), "log": log}), 400
    except zipfile.BadZipFile:
        return jsonify({"error": "zip corrupto o invalido"}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e), "log": log}), 500


# ---------------------------------------------------------------------------
# server.properties
# ---------------------------------------------------------------------------
PROPS_FILE = DATA_DIR / "server.properties"
# Claves editables mostradas en el panel (el resto se preserva sin tocar)
EDITABLE_PROPS = [
    "level-seed", "level-name", "level-type", "difficulty", "gamemode",
    "motd", "max-players", "view-distance", "simulation-distance",
    "pvp", "hardcore", "white-list", "enforce-whitelist", "online-mode",
    "spawn-protection", "allow-nether", "allow-flight", "enable-command-block",
    "spawn-monsters", "force-gamemode",
]


def _read_properties():
    props = {}
    if PROPS_FILE.is_file():
        for line in PROPS_FILE.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                props[k] = v
    return props


def _write_properties(updates):
    lines = PROPS_FILE.read_text().splitlines() if PROPS_FILE.is_file() else []
    seen, out = set(), []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0]
            if k in updates:
                out.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    PROPS_FILE.write_text("\n".join(out) + "\n")


def _level_name():
    return _read_properties().get("level-name", "world")


def _world_path():
    return DATA_DIR / _level_name()


@app.route("/api/properties")
@require_auth
def api_properties_get():
    props = _read_properties()
    return jsonify({
        "editable": {k: props.get(k, "") for k in EDITABLE_PROPS},
        "all_keys": sorted(props.keys()),
    })


@app.route("/api/properties", methods=["POST"])
@require_auth
def api_properties_set():
    updates = (request.json or {}).get("properties", {})
    if not isinstance(updates, dict) or not updates:
        return jsonify({"error": "sin cambios"}), 400
    # Solo permitir claves conocidas para evitar romper el fichero
    updates = {k: str(v) for k, v in updates.items() if k in EDITABLE_PROPS}
    _write_properties(updates)
    if (request.json or {}).get("restart"):
        try:
            get_container().restart()
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": True, "restart_error": str(e), "saved": updates})
    return jsonify({"ok": True, "saved": updates})


# ---------------------------------------------------------------------------
# Backups y descarga del mundo
# ---------------------------------------------------------------------------
def _zip_dir(src: Path, zf: zipfile.ZipFile, base: Path):
    for p in src.rglob("*"):
        if p.is_file():
            zf.write(p, p.relative_to(base))


def _server_running():
    try:
        return get_container().status == "running"
    except Exception:  # noqa: BLE001
        return False


def _make_backup(label=""):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    world = _world_path()
    if not world.is_dir():
        raise RconError("no existe el mundo a respaldar")
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = secure_filename(label) or "backup"
    fname = f"{stem}-{ts}.zip"
    target = BACKUP_DIR / fname

    # Backup en caliente: pausa el guardado y vuelca a disco para evitar
    # regiones a medio escribir. Si el server no responde por RCON, se hace
    # igualmente (mejor un backup imperfecto que ninguno).
    hot = _server_running()
    if hot:
        try:
            rcon_command("save-off")
            rcon_command("save-all flush")
            time.sleep(1)
        except Exception:  # noqa: BLE001
            hot = False
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            _zip_dir(world, z, world.parent)  # rutas relativas: world/...
    finally:
        if hot:
            try:
                rcon_command("save-on")
            except Exception:  # noqa: BLE001
                pass
    return {"name": fname, "size_mb": round(target.stat().st_size / 1048576, 2)}


@app.route("/api/backups")
@require_auth
def api_backups_list():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(BACKUP_DIR.glob("*.zip"), reverse=True):
        st = f.stat()
        out.append({
            "name": f.name,
            "size_mb": round(st.st_size / 1048576, 2),
            "date": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return jsonify({"backups": out})


@app.route("/api/backups", methods=["POST"])
@require_auth
def api_backups_create():
    label = (request.json or {}).get("label", "") if request.is_json else ""
    try:
        return jsonify({"ok": True, **_make_backup(label)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/api/backups/<name>/download")
@require_auth
def api_backups_download(name):
    name = secure_filename(name)
    target = BACKUP_DIR / name
    if not target.is_file():
        return jsonify({"error": "no existe"}), 404
    return send_file(target, as_attachment=True, download_name=name)


@app.route("/api/backups/<name>", methods=["DELETE"])
@require_auth
def api_backups_delete(name):
    name = secure_filename(name)
    target = BACKUP_DIR / name
    if not target.is_file():
        return jsonify({"error": "no existe"}), 404
    target.unlink()
    return jsonify({"ok": True})


@app.route("/api/backups/<name>/restore", methods=["POST"])
@require_auth
def api_backups_restore(name):
    name = secure_filename(name)
    target = BACKUP_DIR / name
    if not target.is_file():
        return jsonify({"error": "no existe"}), 404
    world = _world_path()
    try:
        c = get_container()
        c.stop()
        if world.is_dir():
            shutil.rmtree(world)
        with zipfile.ZipFile(target) as z:
            z.extractall(DATA_DIR)  # el zip contiene la carpeta world/
        c.start()
        return jsonify({"ok": True, "restored": name})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/api/world/download")
@require_auth
def api_world_download():
    world = _world_path()
    if not world.is_dir():
        return jsonify({"error": "no existe el mundo"}), 404
    tmp = Path(tempfile.gettempdir()) / f"{_level_name()}.zip"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        _zip_dir(world, z, world.parent)
    return send_file(tmp, as_attachment=True, download_name=f"{_level_name()}.zip")


@app.route("/api/world/regenerate", methods=["POST"])
@require_auth
def api_world_regenerate():
    body = request.json or {}
    seed = str(body.get("seed", "")).strip()
    level_type = body.get("level_type", "").strip()
    world = _world_path()
    try:
        # Respaldo del mundo actual antes de borrar
        if world.is_dir():
            _make_backup("pre-regen")
        c = get_container()
        c.stop()
        updates = {"level-seed": seed}
        if level_type:
            updates["level-type"] = level_type
        _write_properties(updates)
        if world.is_dir():
            shutil.rmtree(world)
        c.start()
        return jsonify({"ok": True, "seed": seed})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Scheduling: backup y restart automaticos (thread en background)
# ---------------------------------------------------------------------------
DEFAULT_SCHEDULE = {
    "backup_enabled": False, "backup_interval_h": 24,
    "restart_enabled": False, "restart_interval_h": 12,
    "last_backup": 0, "last_restart": 0,
}


def _load_schedule():
    cfg = dict(DEFAULT_SCHEDULE)
    if SCHEDULE_FILE.is_file():
        try:
            cfg.update(json.loads(SCHEDULE_FILE.read_text()))
        except Exception:  # noqa: BLE001
            pass
    return cfg


def _save_schedule(cfg):
    SCHEDULE_FILE.write_text(json.dumps(cfg, indent=2))


@app.route("/api/schedule")
@require_auth
def api_schedule_get():
    return jsonify(_load_schedule())


@app.route("/api/schedule", methods=["POST"])
@require_auth
def api_schedule_set():
    cfg = _load_schedule()
    body = request.json or {}
    for k in ("backup_enabled", "restart_enabled"):
        if k in body:
            cfg[k] = bool(body[k])
    for k in ("backup_interval_h", "restart_interval_h"):
        if k in body:
            try:
                cfg[k] = max(1, int(body[k]))
            except (ValueError, TypeError):
                pass
    _save_schedule(cfg)
    return jsonify(cfg)


def _scheduler_loop():
    while True:
        time.sleep(60)
        try:
            cfg = _load_schedule()
            now = time.time()
            changed = False
            # last_*=0 => recien arrancado: fija baseline sin ejecutar
            if cfg["last_backup"] == 0 or cfg["last_restart"] == 0:
                if cfg["last_backup"] == 0:
                    cfg["last_backup"] = now
                if cfg["last_restart"] == 0:
                    cfg["last_restart"] = now
                _save_schedule(cfg)
                continue
            if cfg["backup_enabled"] and now - cfg["last_backup"] >= cfg["backup_interval_h"] * 3600:
                try:
                    _make_backup("auto")
                except Exception:  # noqa: BLE001
                    pass
                cfg["last_backup"] = now
                changed = True
            if cfg["restart_enabled"] and now - cfg["last_restart"] >= cfg["restart_interval_h"] * 3600:
                try:
                    get_container().restart()
                except Exception:  # noqa: BLE001
                    pass
                cfg["last_restart"] = now
                changed = True
            if changed:
                _save_schedule(cfg)
        except Exception:  # noqa: BLE001
            pass


threading.Thread(target=_scheduler_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Auto-reparar: saca a cuarentena mods client-only que crashean el server
# ---------------------------------------------------------------------------
QUAR_DIR = DATA_DIR / "client-only-removed"


def _jar_for_modid(modid):
    for j in MODS_DIR.glob("*.jar"):
        try:
            with zipfile.ZipFile(j) as z:
                names = z.namelist()
                for toml in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                    if toml in names:
                        txt = z.read(toml).decode("utf-8", "replace")
                        if re.search(r'modId\s*=\s*"%s"' % re.escape(modid), txt):
                            return j
        except Exception:  # noqa: BLE001
            pass
    return None


def _wait_health(c, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        c.reload()
        h = c.attrs["State"].get("Health", {}).get("Status")
        st = c.status
        if h == "healthy" or (not h and st == "running"):
            return "ok"
        if h == "unhealthy" or st in ("exited", "dead"):
            return "bad"
        time.sleep(5)
    return "timeout"


@app.route("/api/heal", methods=["POST"])
@require_auth
def api_heal():
    QUAR_DIR.mkdir(parents=True, exist_ok=True)
    steps, quarantined = [], []
    try:
        c = get_container()
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500
    for _ in range(12):
        c.restart()
        res = _wait_health(c, timeout=180)
        if res == "ok":
            return jsonify({"ok": True, "quarantined": quarantined, "steps": steps})
        logs = c.logs(tail=200).decode("utf-8", "replace")
        logs = re.sub(r"\x1b\[[0-9;]*m", "", logs)
        m = re.findall(r"\(([a-z0-9_]+)\) has failed to load", logs)
        if not m:
            steps.append("no se pudo identificar el mod culpable")
            return jsonify({"ok": False, "quarantined": quarantined, "steps": steps,
                            "hint": "revisa los logs manualmente"}), 200
        modid = m[-1]
        jar = _jar_for_modid(modid)
        if not jar:
            steps.append(f"mod '{modid}' crashea pero no encuentro su .jar")
            return jsonify({"ok": False, "quarantined": quarantined, "steps": steps}), 200
        jar.rename(QUAR_DIR / jar.name)
        quarantined.append(jar.name)
        steps.append(f"cuarentena: {modid} -> {jar.name}")
    return jsonify({"ok": False, "quarantined": quarantined, "steps": steps,
                    "hint": "alcanzado limite de intentos"}), 200


@app.route("/api/logs")
@require_auth
def api_logs():
    try:
        c = get_container()
        tail = request.args.get("tail", "100")
        logs = c.logs(tail=int(tail)).decode("utf-8", errors="replace")
        return jsonify({"logs": logs})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
