# The desktop shell (Roadmap Phase 11)

An Electron window around the daemon. Open a project folder, get a window; the window starts
that project's daemon, loads its cockpit, and stops the daemon when it closes.

## Run it

```bash
cd desktop
npm install
npm start                 # opens on the repository this checkout is in
npm start -- /path/to/a/project
```

`npm start` finds Python in the repository's `.venv` (`ORCHESTRATOR_PYTHON` overrides it) and
spawns:

```
python -m orchestrator --daemon <a free port> --project-root <folder> --no-browser
```

It then waits for that daemon's own handshake — `.orchestrator/daemon.json`, with the port it
was told to use — before loading the page. Nothing is guessed from log output and nothing
sleeps for a fixed time.

## Package it

```bash
npm run dist        # an unpacked build, for trying it
npm run package     # an installer for this platform
```

The package contains the shell only. The engine is still Python in this repository, called as
a module — see "Why Electron", below.

## Why Electron

Roadmap §10.7 left this open until Phase 10 was done, and §10.10 carried it as an open
question. It is decided here, and the reason is narrow: this shell has to embed a terminal
(Phase 10), and Electron's `node-pty` is the mature option for that on Windows, which is where
this project is developed. Tauri would ship a smaller app, but its `portable-pty` route would
add Rust — a language nothing in this project currently uses — to buy a smaller download for a
tool that runs beside a checkout of the repository it drives. That trade did not pay.

The decision is also cheap to revisit: this directory is the entire commitment. `main.js`
spawns a process and loads a URL. Nothing in `orchestrator/` knows this exists.

## What is deliberately not here

* **No engine.** The shell never imports, reimplements, or reaches around the daemon. If you
  deleted this directory, `python -m orchestrator --daemon` would lose a window frame and
  nothing else.
* **No privileged bridge.** `preload.js` exposes one boolean. The page has no Node integration
  and runs sandboxed, so the shell adds no way to act that a daemon route does not govern.
* **The daemon already owns a real pty, in Python.** `terminals.run_captured_pty`
  (`pywinpty`/ConPTY on Windows, the standard library on POSIX) is what lets OpenCode's actual
  interactive `attach` process render in the cockpit's panel with no Antigravity IDE bridge
  needed. This shell doesn't add that — it only loads the page that already has it. What
  *would* be new here is `node-pty` giving a **native window** the same live pty (an actual
  OS console widget instead of a browser `<pre>`), which is still the reason this shell is
  Electron rather than a plain browser tab — but it is a presentation upgrade, not a missing
  capability.
