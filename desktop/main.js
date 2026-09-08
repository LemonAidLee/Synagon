// The desktop shell (Roadmap Phase 11).
//
// Deliberately last, and deliberately thin. Everything this window shows was already proven
// in a browser tab against a running daemon (Phases 8-10); what is added here is the chrome a
// browser cannot give: open a project folder, a window per project, native menus, and a
// daemon whose lifetime is the window's rather than a terminal's.
//
// What this file is allowed to do
// -------------------------------
// * spawn `python -m orchestrator --daemon <port> --project-root <folder> --no-browser`,
// * wait for that daemon's own handshake file to appear, and
// * load the cockpit it serves.
//
// What it must never do: reach into the project, call the engine, or hold any state of its
// own about a run. Every fact still comes from the daemon, which still gets it from the
// stores. If this shell were deleted, `--daemon` in a terminal would lose nothing but a
// window frame - and that is the test of whether the split in Roadmap §10.7 was kept.

// Electron degrades to a plain Node interpreter - `require("electron")` returns a path
// string instead of the {app, BrowserWindow, ...} API - whenever ELECTRON_RUN_AS_NODE is set
// in its environment. Some dev tooling sets that globally so it can reuse Electron's bundled
// Node as a script runner, and it is inherited by anything launched from the same shell,
// including this app. Detected here rather than assumed, and fixed by re-executing this same
// binary with the variable cleared, instead of failing later with `app` mysteriously
// undefined (exactly what happened during development, from a shell that had it set).
if (process.env.ELECTRON_RUN_AS_NODE) {
  const { spawnSync } = require("child_process");
  const cleanEnv = { ...process.env };
  delete cleanEnv.ELECTRON_RUN_AS_NODE;
  const result = spawnSync(process.execPath, process.argv.slice(1), {
    env: cleanEnv,
    stdio: "inherit",
  });
  process.exit(result.status === null ? 1 : result.status);
}

const { app, BrowserWindow, Menu, dialog, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const net = require("net");
const path = require("path");

// The repository this shell ships inside. The daemon is a module in it, not a bundled binary:
// the engine stays Python, exactly as it was.
const REPO_ROOT = path.resolve(__dirname, "..");

/** One open project: its window, its daemon process, and the folder it was opened on. */
const projects = new Map(); // window id -> { window, child, projectRoot, port }

function pythonCommand() {
  // The project's own virtualenv first, because that is where its dependencies are. An
  // explicit override wins, so a machine with an unusual layout is not stuck.
  if (process.env.ORCHESTRATOR_PYTHON) return process.env.ORCHESTRATOR_PYTHON;
  const candidates =
    process.platform === "win32"
      ? [path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")]
      : [path.join(REPO_ROOT, ".venv", "bin", "python")];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return process.platform === "win32" ? "python" : "python3";
}

/** Ask the OS for a port nothing is using, so two open projects never collide. */
function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

/**
 * Reserve a port, or explain why there is no daemon to start.
 *
 * `openProject` must not be able to reject before it has a window to report into. A `freePort`
 * failure - the machine has no loopback port left to give - is the same class of problem as a
 * daemon that never appears, so it surfaces the same dialog and lets the caller carry on,
 * instead of an unhandled promise rejection in the Electron main process.
 */
async function reservePort(projectRoot) {
  try {
    return await freePort();
  } catch (err) {
    await dialog.showMessageBox(null, {
      type: "error",
      title: "The daemon did not start",
      message: `Could not reserve a port for ${projectRoot}.`,
      detail: String(err && err.message ? err.message : err),
    });
    return null;
  }
}

const handshakeFile = (projectRoot) =>
  path.join(projectRoot, ".orchestrator", "daemon.json");

/**
 * Wait until the daemon has written its handshake for the port we asked for.
 *
 * The handshake is the daemon's own signal that it is serving - not a guess from a log line,
 * and not a fixed sleep. A stale file from a previous run is ignored because the port has to
 * match the one this launch was given.
 */
function awaitDaemon(projectRoot, port, timeoutMs = 30000) {
  const started = Date.now();
  const file = handshakeFile(projectRoot);
  return new Promise((resolve, reject) => {
    const tick = () => {
      try {
        const payload = JSON.parse(fs.readFileSync(file, "utf-8"));
        if (Number(payload.port) === Number(port)) return resolve(payload);
      } catch (err) {
        /* not written yet */
      }
      if (Date.now() - started > timeoutMs) {
        return reject(new Error("the daemon did not start within 30 seconds"));
      }
      setTimeout(tick, 200);
    };
    tick();
  });
}

async function openProject(projectRoot) {
  // A daemon was briefly seen spawned twice for one launch, of unconfirmed origin. This is
  // not decorative: if it recurs, the timestamp and call count pin down whether it is one
  // call whose child process the OS lists twice, two genuinely separate calls (from where?),
  // or a double invocation of `whenReady`/`activate` this file did not anticipate.
  openProject._calls = (openProject._calls || 0) + 1;
  console.log(
    `[main] openProject #${openProject._calls} for ${projectRoot} at ${new Date().toISOString()}`
  );

  const port = await reservePort(projectRoot);
  if (port === null) return null;

  // A stale handshake would otherwise be mistaken for this launch's.
  try {
    fs.unlinkSync(handshakeFile(projectRoot));
  } catch (err) {
    /* there was none */
  }

  const child = spawn(
    pythonCommand(),
    [
      "-m",
      "orchestrator",
      "--daemon",
      String(port),
      "--project-root",
      projectRoot,
      "--no-browser",
    ],
    { cwd: REPO_ROOT, stdio: ["ignore", "pipe", "pipe"] }
  );

  let startupLog = "";
  const remember = (chunk) => {
    const text = chunk.toString();
    startupLog = (startupLog + text).slice(-4000);
    // Also surfaced live in this process's own console (visible to whoever ran `npm start`
    // from a terminal) - the daemon's own stdout/stderr previously only reached a person via
    // the error dialog on an unexpected exit, which meant a crash between here and there
    // left no visible trace at all.
    process.stdout.write(`[daemon:${port}] ${text}`);
  };
  child.stdout.on("data", remember);
  child.stderr.on("data", remember);

  const window = new BrowserWindow({
    width: 1360,
    height: 880,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: "#10131a",
    title: `Orchestrator — ${path.basename(projectRoot)}`,
    show: false,
    webPreferences: {
      // The cockpit is a plain page served over loopback; it needs no Node, and giving it
      // none is what keeps the shell from becoming an extra thing that can act.
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  projects.set(window.id, { window, child, projectRoot, port });

  // A link to a pull request belongs in the person's own browser, not in this window.
  window.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  try {
    await awaitDaemon(projectRoot, port);
    await window.loadURL(`http://127.0.0.1:${port}/`);
    window.show();
  } catch (err) {
    window.show();
    dialog.showMessageBox(window, {
      type: "error",
      title: "The daemon did not start",
      message: `Could not start the orchestrator daemon for ${projectRoot}.`,
      detail: `${err.message}\n\n${startupLog}`,
    });
  }

  child.on("exit", (code, signal) => {
    console.log(
      `[main] daemon on port ${port} exited: code=${code} signal=${signal} at ${new Date().toISOString()}`
    );
    if (!window.isDestroyed() && code !== 0) {
      dialog.showMessageBox(window, {
        type: "warning",
        title: "The daemon stopped",
        message: "The orchestrator daemon for this project exited.",
        detail: `Exit code ${code}.\n\n${startupLog}`,
      });
    }
  });

  window.on("closed", () => {
    const entry = projects.get(window.id);
    projects.delete(window.id);
    if (entry && entry.child && !entry.child.killed) entry.child.kill();
  });

  return window;
}

async function chooseProject(parent) {
  const result = await dialog.showOpenDialog(parent || null, {
    title: "Open a project",
    properties: ["openDirectory"],
    buttonLabel: "Open",
  });
  if (result.canceled || !result.filePaths.length) return null;
  return openProject(result.filePaths[0]);
}

function buildMenu() {
  const template = [
    {
      label: "File",
      submenu: [
        {
          label: "Open Project Folder…",
          accelerator: "CmdOrCtrl+O",
          click: (_item, window) => chooseProject(window),
        },
        {
          label: "New Window on This Project",
          accelerator: "CmdOrCtrl+Shift+N",
          click: (_item, window) => {
            const entry = window ? projects.get(window.id) : null;
            if (entry) openProject(entry.projectRoot);
            else chooseProject(window);
          },
        },
        { type: "separator" },
        { role: "close" },
        { role: "quit" },
      ],
    },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "forceReload" },
        { role: "toggleDevTools" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    {
      label: "Go",
      submenu: [
        {
          label: "Cockpit",
          click: (_item, window) => window && window.loadURL(urlFor(window, "/")),
        },
        {
          label: "Office",
          click: (_item, window) => window && window.loadURL(urlFor(window, "/office")),
        },
        {
          label: "Design the team",
          click: (_item, window) => window && window.loadURL(urlFor(window, "/design")),
        },
      ],
    },
    { role: "windowMenu" },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function urlFor(window, route) {
  const entry = projects.get(window.id);
  return entry ? `http://127.0.0.1:${entry.port}${route}` : "about:blank";
}

app.whenReady().then(async () => {
  console.log(`[main] whenReady fired at ${new Date().toISOString()}`);
  buildMenu();
  // Opened on a folder given on the command line, on this repository, or on whatever the
  // person picks - in that order, so `npm start` in a checkout just works.
  const argument = process.argv.slice(2).find((a) => !a.startsWith("-"));
  const initial = argument ? path.resolve(argument) : REPO_ROOT;
  if (fs.existsSync(path.join(initial, "orchestrator.yaml"))) await openProject(initial);
  else await chooseProject(null);
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  for (const entry of projects.values()) {
    if (entry.child && !entry.child.killed) entry.child.kill();
  }
});

app.on("activate", () => {
  console.log(`[main] activate fired at ${new Date().toISOString()}, windows=${BrowserWindow.getAllWindows().length}`);
  if (BrowserWindow.getAllWindows().length === 0) chooseProject(null);
});
