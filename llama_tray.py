#!/usr/bin/env python3
"""
Tray icon and controls for a locally running llama.cpp router (`llama serve`).

Usage:
    ./llama_tray.py                tray for a router you started yourself
    ./llama_tray.py --start        start the router first if it is not running
    ./llama_tray.py --status       print one poll as JSON and exit (no display)
    ./llama_tray.py --stop-server  stop the router and exit

The first run creates a virtualenv beside this script, installs pystray,
pillow and requests into it and re-execs, so a fresh clone needs nothing but
python3 and llama.cpp. On Linux the tray backend also wants a few system
packages that pip cannot supply - see README.md.

States:
    offline   - the router itself is unreachable (not started / crashed)
    unloaded  - router running, no model loaded
    loading   - router running, a model is loading or downloading
    loaded    - router running, a model is loaded, nothing generating
    running   - router running, a loaded model is actively processing

Router API, verified against llama.cpp b10630 (`llama serve`):

  GET  /v1/models         -> {"data": [{"id": ..., "aliases": [...],
                               "status": {"value": "unloaded"|"loading"|"loaded"|...}}]}
      In router mode this lists EVERY model the router knows about, not just
      the resident ones, so a non-empty `data` says nothing about whether
      anything is loaded - only `status.value` does.

  GET  /slots?model=<id>  -> [{..., "is_processing": bool}, ...]
      The `model` query parameter is REQUIRED in router mode (without it:
      400 "model name is missing from the request"), and asking about a model
      that is not resident makes the router AUTOLOAD it - which can kick off a
      multi-gigabyte download. Only ever query models already reported loaded.

  POST /models/load       {"model": "<exact id>"} -> {"success": true}, async
  POST /models/unload     {"model": "<exact id>"}
      The id must match /v1/models exactly; an unknown one comes back as a
      404 "File Not Found".

  GET  /props             -> {"role": "router", "max_instances": N, ...}
"""

# Standard library only above ensure_deps(): the third-party imports below it
# are what the bootstrap exists to make importable.
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEP_MODULES = ("pystray", "PIL", "requests")
FALLBACK_REQUIREMENTS = ("pystray", "pillow", "requests")
BOOTSTRAP_FLAG = "LLAMA_TRAY_BOOTSTRAPPED"


def _venv_python(venv):
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")


def _venv_dir():
    """Reuse whichever virtualenv is already here - `venv/` is what earlier
    versions of this script used - and otherwise create `.venv/`."""
    for name in ("venv", ".venv"):
        if _venv_python(HERE / name).exists():
            return HERE / name
    return HERE / ".venv"


def _missing_here():
    """Which dependencies this interpreter cannot import.

    find_spec rather than a real import: importing pystray opens a connection
    to the display, which would make --status unusable on a headless box."""
    missing = []
    for module in DEP_MODULES:
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(module)
        except (ImportError, ValueError):
            missing.append(module)
    return missing


def _missing_in(python):
    """The same question, asked of another interpreter."""
    probe = (
        "import importlib.util as u;"
        f"print(' '.join(m for m in {DEP_MODULES!r} if u.find_spec(m) is None))"
    )
    done = subprocess.run([str(python), "-c", probe], capture_output=True, text=True)
    return done.stdout.split() if done.returncode == 0 else list(DEP_MODULES)


def ensure_deps():
    """Make the third-party imports below work, then return.

    If they already do - a system-wide install, or the virtualenv we have
    already re-exec'd into - this does nothing at all. Otherwise it creates a
    virtualenv beside this script, installs into it and re-execs. The
    environment flag makes that happen at most once, so a failed install
    reports itself instead of looping."""
    if not _missing_here():
        return
    if os.environ.get(BOOTSTRAP_FLAG):
        sys.exit(
            "llama_tray: still cannot import "
            + ", ".join(_missing_here())
            + f" after setting up {_venv_dir()} - see README.md"
        )

    venv = _venv_dir()
    python = _venv_python(venv)
    if not python.exists():
        print(f"llama_tray: creating virtualenv in {venv}", file=sys.stderr)
        # --system-site-packages so pystray can reach the system PyGObject and
        # GTK typelibs it needs for the appindicator backend; pip cannot
        # supply those.
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
            check=True,
        )

    if _missing_in(python):
        requirements = HERE / "requirements.txt"
        spec = (["-r", str(requirements)] if requirements.exists()
                else list(FALLBACK_REQUIREMENTS))
        print(f"llama_tray: installing dependencies into {venv}", file=sys.stderr)
        subprocess.run([str(python), "-m", "pip", "install", "--quiet", *spec], check=True)

    os.environ[BOOTSTRAP_FLAG] = "1"
    os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])


ensure_deps()

import requests                          # noqa: E402  (after the bootstrap)
from PIL import Image, ImageDraw         # noqa: E402

# pystray is imported later still, in run_tray(): it connects to the display at
# import time, so importing it here would make --status unusable headless
# (over ssh, from a systemd unit, and so on).
Menu = MenuItem = None

# The launch script binds the server to 0.0.0.0, but the tray always talks to
# it over loopback. The port falls back to LLAMA_ARG_PORT, which the tray
# inherits from whatever started the router, so one setting covers both.
HOST = os.environ.get("LLAMA_TRAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("LLAMA_TRAY_PORT")
           or os.environ.get("LLAMA_ARG_PORT")
           or 8080)
BASE_URL = f"http://{HOST}:{PORT}"

POLL_SECONDS = 2.0
HTTP_TIMEOUT = 1.5          # per request - it is loopback, this is generous
ACTION_TIMEOUT = 10.0       # load/unload return as soon as the work is queued
STOP_GRACE_SECONDS = 10.0   # SIGTERM -> SIGKILL window when stopping the router

# Arguments for "Start server" and --start. Edit to taste: --models-max 1 keeps
# a single model resident at a time.
SERVE_ARGS = ["--models-max", "1"]
MCP_CONFIG = Path.home() / ".config" / "llama" / "mcp-config.json"

# Applied only where the environment is silent, so a tray started by
# llama-start.sh uses that script's bind settings rather than contradicting
# them. Standalone, it binds loopback only - llama.cpp's own default.
SERVER_ENV_DEFAULTS = {"LLAMA_ARG_HOST": "127.0.0.1", "LLAMA_ARG_PORT": str(PORT)}
SERVER_LOG = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "llama" / "serve.log"

LOADED = "loaded"
BUSY_VALUES = ("loading", "downloading")

STATE_COLORS = {
    "offline":  (178, 34, 34),    # dark red   - router unreachable
    "unloaded": (140, 140, 140),  # gray       - router up, nothing loaded
    "loading":  (55, 130, 235),   # blue       - loading / downloading
    "loaded":   (60, 180, 75),    # green      - model resident, idle
    "running":  (230, 160, 20),   # amber      - actively generating
}


def make_dot(color, size=64, scale=4):
    """A dot with a translucent dark rim, so it stays legible on light and
    dark panels alike. Drawn oversized and downsampled because PIL's ellipse
    is not antialiased."""
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    pad = 6 * scale
    ImageDraw.Draw(img).ellipse(
        (pad, pad, big - pad, big - pad),
        fill=color + (255,),
        outline=(0, 0, 0, 90),
        width=2 * scale,
    )
    return img.resize((size, size), Image.LANCZOS)


ICONS = {state: make_dot(color) for state, color in STATE_COLORS.items()}


@dataclass(frozen=True)
class Status:
    """An immutable snapshot of what the router is doing. The poll thread
    publishes a new one; menu callbacks on the GTK thread read whichever is
    current. Rebinding a name is atomic, so no lock is needed as long as this
    stays immutable."""
    state: str = "offline"
    models: tuple = ()   # ((model_id, label, status_value), ...)
    active: str = ""     # label(s) of the resident model(s)
    detail: str = ""     # secondary line for the menu

    @property
    def online(self):
        return self.state != "offline"


STATUS = Status()

_wake = threading.Event()      # poll now instead of waiting out the interval
_stopping = threading.Event()

# Never let http_proxy/HTTPS_PROXY from the environment sit between the tray
# and a loopback server, and reuse the connection across polls.
_session = requests.Session()
_session.trust_env = False


def _get(path, **params):
    resp = _session.get(BASE_URL + path, params=params or None, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _post(path, payload):
    resp = _session.post(BASE_URL + path, json=payload, timeout=ACTION_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def is_processing(model_id):
    """Whether any slot of a resident model is mid-request.

    Only call this for a model already reported as loaded - see the /slots
    note in the module docstring."""
    try:
        slots = _get("/slots", model=model_id)
    except (requests.RequestException, ValueError):
        return False
    if isinstance(slots, dict):              # some builds wrap the list
        slots = slots.get("slots") or []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        if "is_processing" in slot:
            if slot["is_processing"]:
                return True
        elif slot.get("state") not in (None, 0, "idle"):   # pre-is_processing builds
            return True
    return False


def display_label(model_id, aliases):
    """The preset alias when there is one, otherwise a trimmed id - the org
    prefix and the -GGUF marker only make the menu wide."""
    if aliases:
        return aliases[0]
    return model_id.rsplit("/", 1)[-1].replace("-GGUF", "").replace("_GGUF", "")


def poll_status():
    """One full state read: /v1/models, plus /slots for resident models only."""
    try:
        data = _get("/v1/models").get("data") or []
    except (requests.RequestException, ValueError):
        return Status()

    models, resident, busy = [], [], []
    for entry in data:
        model_id = entry.get("id")
        if not model_id:
            continue
        label = display_label(model_id, entry.get("aliases") or [])
        # A plain (non-router) llama-server omits `status` entirely and only
        # ever lists the model it already has loaded.
        value = (entry.get("status") or {}).get("value", LOADED)
        models.append((model_id, label, value))
        if value == LOADED:
            resident.append((model_id, label))
        elif value in BUSY_VALUES:
            busy.append((label, value))

    models = tuple(models)
    if busy:
        labels = ", ".join(label for label, _ in busy)
        return Status("loading", models, labels, busy[0][1])
    if not resident:
        return Status("unloaded", models, "", "no model loaded")

    labels = ", ".join(label for _, label in resident)
    if any(is_processing(model_id) for model_id, _ in resident):
        return Status("running", models, labels, "generating")
    return Status("loaded", models, labels, "idle")


def server_command():
    """`llama serve` plus the MCP config only when there is one: the router
    exits with status 1 if --mcp-servers-config names a file that does not
    exist, which a fresh clone would hit on its very first run."""
    cmd = ["llama", "serve", *SERVE_ARGS]
    if MCP_CONFIG.exists():
        cmd += ["--mcp-servers-config", str(MCP_CONFIG)]
    return cmd


def _is_router(argv):
    return (len(argv) >= 2
            and os.path.basename(argv[0]) == "llama"
            and argv[1] == "serve")


def _pids_from_proc(uid):
    pids = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            with open(f"/proc/{entry.name}/cmdline", "rb") as handle:
                raw = handle.read().split(b"\0")[:2]
        except OSError:
            continue                     # exited between scandir and open
        if _is_router([part.decode("utf-8", "replace") for part in raw]):
            pids.append(int(entry.name))
    return pids


def _pids_from_ps(uid):
    """macOS, and anything else POSIX without /proc."""
    try:
        done = subprocess.run(["ps", "-Ao", "pid=,uid=,args="],
                              capture_output=True, text=True)
    except OSError:
        return []
    pids = []
    for line in done.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[1] == str(uid) and _is_router(fields[2].split()):
            pids.append(int(fields[0]))
    return pids


def server_pids():
    """PIDs of the router and of the per-model instances it spawns - both run
    as `llama serve ...`.

    Matched on argv[0]'s basename plus argv[1] rather than with
    `pkill -f "llama serve"`: a full-command-line match also catches any
    shell, editor or grep that merely mentions that string, and stop_server()
    would then signal those. Restricted to our own processes, so a stray match
    elsewhere on a multi-user box cannot be signalled either."""
    uid = os.getuid()
    if Path("/proc/self/cmdline").exists():
        return _pids_from_proc(uid)
    return _pids_from_ps(uid)


def start_server():
    if server_pids():
        return
    cmd = server_command()
    if shutil.which(cmd[0]) is None:
        raise RuntimeError(f"`{cmd[0]}` is not on PATH - install llama.cpp first")
    SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(SERVER_LOG, "ab") as log:
        log.write(f"\n=== tray start {time.strftime('%F %T')} ===\n".encode())
        log.flush()
        # start_new_session so the router outlives the tray.
        subprocess.Popen(
            cmd,
            env={**SERVER_ENV_DEFAULTS, **os.environ},
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def stop_server():
    """SIGTERM first, so the router unloads its models and reaps the per-model
    instances it spawned; SIGKILL only whatever is still alive after that."""
    def signal_all(sig):
        for pid in server_pids():
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    signal_all(signal.SIGTERM)
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    while time.monotonic() < deadline and server_pids():
        time.sleep(0.25)
    signal_all(getattr(signal, "SIGKILL", signal.SIGTERM))


def restart_server():
    stop_server()
    start_server()


def load_model(model_id):
    _post("/models/load", {"model": model_id})


def unload_model(model_id):
    _post("/models/unload", {"model": model_id})


def unload_all():
    for model_id, _, value in STATUS.models:
        if value == LOADED:
            unload_model(model_id)


def open_webui():
    webbrowser.open(BASE_URL)


def action(func, *args):
    """Wrap a command as a menu callback. Menu callbacks run on the GTK main
    thread, so the work goes to a worker to keep the menu responsive, and the
    poll thread is woken to pick up the result."""
    def run():
        try:
            func(*args)
        except Exception as exc:                     # never kill the menu
            print(f"llama_tray: {func.__name__} failed: {exc}", file=sys.stderr)
        finally:
            _wake.set()

    def callback(icon, item):
        threading.Thread(target=run, daemon=True).start()

    return callback


def model_items():
    """Submenu of every model the router knows about. Clicking a resident one
    unloads it, clicking any other loads it."""
    status = STATUS
    if not status.models:
        yield MenuItem("(no models)", None, enabled=False)
        return
    for model_id, label, value in status.models:
        resident = value == LOADED
        text = label if value in (LOADED, "unloaded") else f"{label} ({value})"
        yield MenuItem(
            text,
            action(unload_model if resident else load_model, model_id),
            checked=lambda item, resident=resident: resident,
        )


def menu_items():
    status = STATUS
    yield MenuItem(f"llama.cpp router - {status.state}", None, enabled=False)
    if status.detail:
        summary = f"{status.active} - {status.detail}" if status.active else status.detail
        yield MenuItem(f"   {summary}", None, enabled=False)

    if status.online:
        yield Menu.SEPARATOR
        yield MenuItem("Models", Menu(model_items))
        if status.state in (LOADED, "running"):
            yield MenuItem("Unload all models", action(unload_all))
        yield Menu.SEPARATOR
        yield MenuItem("Open Web UI", action(open_webui))
        yield Menu.SEPARATOR
        yield MenuItem("Restart server", action(restart_server))
        yield MenuItem("Stop server", action(stop_server))
    else:
        yield Menu.SEPARATOR
        yield MenuItem("Start server", action(start_server))

    yield Menu.SEPARATOR
    yield MenuItem("Quit tray (leaves the server running)", quit_tray)


def quit_tray(icon, item):
    _stopping.set()
    _wake.set()
    icon.stop()


def apply_status(icon, status):
    global STATUS
    previous, STATUS = STATUS, status

    if status.state != previous.state:
        icon.icon = ICONS[status.state]

    title = f"llama.cpp router - {status.state}"
    if status.active:
        title += f" ({status.active})"
    icon.title = title

    # The appindicator backend cannot build the menu on demand, so a menu that
    # reflects live state has to be rebuilt whenever that state changes.
    if (status.state, status.active, status.models) != (
        previous.state, previous.active, previous.models
    ):
        icon.update_menu()


def poll_loop(icon):
    """pystray's setup callback: runs in its own thread once the icon exists.
    A custom setup has to make the icon visible itself."""
    icon.visible = True
    while not _stopping.is_set():
        try:
            apply_status(icon, poll_status())
        except Exception as exc:     # a dead poll thread means a frozen icon
            print(f"llama_tray: poll failed: {exc}", file=sys.stderr)
        _wake.wait(POLL_SECONDS)
        _wake.clear()


def stop_server_cli():
    """`--stop-server`, so a launch script gets this exact process matching and
    the SIGTERM-then-SIGKILL sequence rather than its own pkill."""
    pids = server_pids()
    stop_server()
    print(f"stopped {len(pids)} llama serve process(es)")


def print_status():
    status = poll_status()
    print(json.dumps({
        "state": status.state,
        "active": status.active,
        "detail": status.detail,
        "models": [
            {"id": model_id, "label": label, "status": value}
            for model_id, label, value in status.models
        ],
    }, indent=2))


def run_tray():
    global Menu, MenuItem
    import pystray
    from pystray import Menu, MenuItem

    icon = pystray.Icon(
        "llama-router",
        ICONS["offline"],
        "llama.cpp router - offline",
        menu=Menu(menu_items),
    )
    icon.run(setup=poll_loop)


def main():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__.strip())
    elif "--status" in args:
        print_status()
    elif "--stop-server" in args:
        stop_server_cli()
    else:
        if "--start" in args:
            start_server()
        run_tray()


if __name__ == "__main__":
    main()
