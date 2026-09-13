# llama-tray

A system tray icon for a locally running [llama.cpp](https://github.com/ggml-org/llama.cpp)
router (`llama serve`), with a menu to load and unload models and to start,
restart or stop the router.

The icon colour is the state at a glance:

| colour | state      | meaning                                        |
| ------ | ---------- | ---------------------------------------------- |
| red    | `offline`  | the router is unreachable (not started/crashed) |
| grey   | `unloaded` | router up, no model loaded                      |
| blue   | `loading`  | a model is loading or downloading               |
| green  | `loaded`   | a model is resident and idle                    |
| amber  | `running`  | a loaded model is processing a request          |

## Quick start

```sh
git clone git@github.com:mttstwrt/llama.cpp-tray-icon.git && cd llama-tray
./llama-start.sh
```

That is all. The first run creates a virtualenv beside the script, installs its
three Python dependencies into it and re-execs; later runs start in about a
tenth of a second. `llama-start.sh` is plain POSIX `sh`, so it behaves the same
from bash, zsh, fish or dash — and it is only a wrapper, so
`python3 llama_tray.py --start` does exactly the same thing.

## Requirements

- **llama.cpp** built with the `llama` CLI, on your `PATH`. Check with
  `llama serve --help`.
- **Python 3.9+**. `pystray`, `pillow` and `requests` are installed for you.
- A desktop with a system tray. On Linux, `pystray` prefers the AppIndicator
  backend, which needs a few things pip cannot provide:

  | distro        | packages                                                     |
  | ------------- | ------------------------------------------------------------ |
  | Arch          | `python-gobject gtk3 libayatana-appindicator`                 |
  | Debian/Ubuntu | `python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1`    |
  | Fedora        | `python3-gobject gtk3 libayatana-appindicator-gtk3`            |

  The Arch names are verified; the others are the usual equivalents. Without
  these `pystray` falls back to its X11 backend, which needs XWayland and does
  not appear in most Wayland panels. The virtualenv is created with
  `--system-site-packages` so it can see them.

macOS should work; the process control is POSIX-only, so Windows is untested.

## Usage

```
./llama_tray.py                 tray for a router you started yourself
./llama_tray.py --start         start the router first if it is not running
./llama_tray.py --status        print one poll as JSON and exit (no display needed)
./llama_tray.py --stop-server   stop the router and exit
```

`--status` is the quickest way to see whether the endpoint handling still
matches your llama.cpp build — it needs no display, so it works over ssh.

Quitting the tray leaves the router running; "Stop server" is a separate menu
item.

## Configuration

Everything lives in the constants at the top of `llama_tray.py`:

- `HOST` / `PORT` — where the tray looks for the router. The port falls back to
  `LLAMA_ARG_PORT`, which the tray inherits from whatever started the router,
  so setting it in one place is enough.
- `SERVE_ARGS` — arguments for "Start server". `--models-max 1` keeps one model
  resident at a time.
- `MCP_CONFIG` — passed as `--mcp-servers-config` only if the file exists; the
  router exits at startup if it is given a path that does not.
- `POLL_SECONDS` — how often the router is polled.

The router's own output goes to `${XDG_STATE_HOME:-~/.local/state}/llama/serve.log`.

Models themselves are configured on the llama.cpp side, in
`~/.config/llama/my-models.ini`; anything the router lists in `/v1/models`
shows up in the Models submenu, using its preset alias when it has one.

## Notes on the router API

`llama_tray.py`'s module docstring records what the router endpoints actually
return, verified against build b10630 — including two that are easy to get
wrong: in router mode `/v1/models` lists every *known* model rather than the
loaded ones, and `/slots` requires a `?model=` parameter and will **autoload**
(and potentially download) a model you ask about that is not resident.
