const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const vscode = require('vscode');

let server = null;
// True only while THIS extension host holds the port. Every IDE window activates the bridge,
// and only one of them can listen.
let listening = false;
let retryTimer = null;
let disposed = false;
const PORT = parseInt(process.env.ANTIGRAVITY_TERMINAL_BRIDGE_PORT || '', 10) || 49182;
const PORT_FILE = process.env.ANTIGRAVITY_TERMINAL_BRIDGE_PORT_FILE
    || path.join(os.homedir(), '.antigravity-ide', 'terminal_bridge.json');
// How long a window that lost the race for the port waits before trying again. Measured: the
// window that held the port closed, and the one left open had given up on its only attempt
// (EADDRINUSE), so no window served the bridge until the IDE was restarted.
const RETRY_MS = parseInt(process.env.ANTIGRAVITY_TERMINAL_BRIDGE_RETRY_MS || '', 10) || 5000;

function stop() {
    disposed = true;
    if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
    }
    if (server) {
        try { server.close(); } catch (e) {}
        server = null;
    }
    // The port file describes whoever is listening. A window that never got the port must not
    // delete the file the listening window wrote.
    if (listening) {
        listening = false;
        try {
            if (fs.existsSync(PORT_FILE)) {
                fs.unlinkSync(PORT_FILE);
            }
        } catch (e) {}
    }
}

function activate(context) {
    console.log('[TerminalBridge] Activating Antigravity Terminal Bridge...');
    disposed = false;

    // Close any existing server
    if (server) {
        try { server.close(); } catch (e) {}
    }

    server = http.createServer(async (req, res) => {
        // Set CORS headers
        res.setHeader('Access-Control-Allow-Origin', '*');
        res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
        res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

        if (req.method === 'OPTIONS') {
            res.writeHead(204);
            res.end();
            return;
        }

        const url = new URL(req.url, `http://localhost:${PORT}`);

        if (req.method === 'GET' && url.pathname === '/health') {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({
                status: 'ok',
                bridge: 'antigravity_integrated',
                version: '1.0.0',
                ide: vscode.version,
                shell: vscode.env.shell
            }));
            return;
        }

        if (req.method === 'POST' && url.pathname === '/create_terminal') {
            let body = '';
            req.on('data', chunk => { body += chunk; });
            req.on('end', () => {
                try {
                    const data = JSON.parse(body);
                    const title = data.title || 'LangGraph Agent';
                    const cwd = data.cwd || undefined;
                    const command = data.command || '';
                    const env = data.env || undefined;

                    // Reuse existing terminal with same name or create new one
                    let term = vscode.window.terminals.find(t => t.name === title);
                    if (term && data.reuse) {
                        term.show(false);
                    } else {
                        term = vscode.window.createTerminal({
                            name: title,
                            cwd: cwd,
                            env: env
                        });
                        term.show(false);
                    }

                    if (command) {
                        term.sendText(command);
                    }

                    res.writeHead(200, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({
                        success: true,
                        title: title,
                        cwd: cwd
                    }));
                } catch (err) {
                    res.writeHead(500, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({
                        success: false,
                        error: String(err)
                    }));
                }
            });
            return;
        }

        if (req.method === 'POST' && url.pathname === '/focus_terminal') {
            let body = '';
            req.on('data', chunk => { body += chunk; });
            req.on('end', () => {
                try {
                    const data = JSON.parse(body);
                    const title = data.title;
                    const term = vscode.window.terminals.find(t => t.name === title);
                    if (term) {
                        term.show(false);
                        res.writeHead(200, { 'Content-Type': 'application/json' });
                        res.end(JSON.stringify({ success: true, found: true }));
                    } else {
                        res.writeHead(404, { 'Content-Type': 'application/json' });
                        res.end(JSON.stringify({ success: false, found: false }));
                    }
                } catch (err) {
                    res.writeHead(500, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({ success: false, error: String(err) }));
                }
            });
            return;
        }

        if (req.method === 'POST' && url.pathname === '/close_terminal') {
            let body = '';
            req.on('data', chunk => { body += chunk; });
            req.on('end', () => {
                try {
                    const data = JSON.parse(body);
                    const title = data.title;
                    const term = vscode.window.terminals.find(t => t.name === title);
                    if (term) {
                        term.dispose();
                        res.writeHead(200, { 'Content-Type': 'application/json' });
                        res.end(JSON.stringify({ success: true, closed: true }));
                    } else {
                        res.writeHead(200, { 'Content-Type': 'application/json' });
                        res.end(JSON.stringify({ success: true, closed: false, message: 'Terminal not found' }));
                    }
                } catch (err) {
                    res.writeHead(500, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({ success: false, error: String(err) }));
                }
            });
            return;
        }

        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Endpoint not found' }));
    });

    server.on('listening', () => {
        listening = true;
        console.log(`[TerminalBridge] Listening on http://127.0.0.1:${PORT}`);
        try {
            fs.mkdirSync(path.dirname(PORT_FILE), { recursive: true });
            fs.writeFileSync(PORT_FILE, JSON.stringify({
                port: PORT,
                host: '127.0.0.1',
                pid: process.pid,
                started_at: new Date().toISOString()
            }, null, 2));
        } catch (e) {
            console.error('[TerminalBridge] Failed to write port file:', e);
        }
    });

    server.on('error', (err) => {
        if (err && err.code === 'EADDRINUSE' && !disposed) {
            // Another window serves the bridge. Keep trying, so that when it closes this one
            // takes over instead of leaving no bridge at all.
            console.log(`[TerminalBridge] Port ${PORT} in use by another window; retrying in ${RETRY_MS}ms`);
            retryTimer = setTimeout(() => {
                retryTimer = null;
                if (!disposed && server && !listening) {
                    try { server.close(); } catch (e) {}
                    server.listen(PORT, '127.0.0.1');
                }
            }, RETRY_MS);
            return;
        }
        console.error('[TerminalBridge] Server error:', err);
    });

    server.listen(PORT, '127.0.0.1');

    context.subscriptions.push({ dispose: stop });
}

function deactivate() {
    stop();
}

module.exports = {
    activate,
    deactivate
};
