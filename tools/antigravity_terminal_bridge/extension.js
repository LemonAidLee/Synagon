const http = require('http');
const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

let server = null;
const PORT = 49182;
const PORT_FILE = 'C:/Users/Asus/.antigravity-ide/terminal_bridge.json';

function activate(context) {
    console.log('[TerminalBridge] Activating Antigravity Terminal Bridge...');

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

    server.listen(PORT, '127.0.0.1', () => {
        console.log(`[TerminalBridge] Listening on http://127.0.0.1:${PORT}`);
        try {
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
        console.error('[TerminalBridge] Server error:', err);
    });

    context.subscriptions.push({
        dispose: () => {
            if (server) {
                try { server.close(); } catch (e) {}
                server = null;
            }
            try {
                if (fs.existsSync(PORT_FILE)) {
                    fs.unlinkSync(PORT_FILE);
                }
            } catch (e) {}
        }
    });
}

function deactivate() {
    if (server) {
        try { server.close(); } catch (e) {}
        server = null;
    }
    try {
        if (fs.existsSync(PORT_FILE)) {
            fs.unlinkSync(PORT_FILE);
        }
    } catch (e) {}
}

module.exports = {
    activate,
    deactivate
};
