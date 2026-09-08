#!/usr/bin/env python
"""Drive a running daemon from a script instead of a terminal invocation (Roadmap Phase 8).

This exists to prove one thing: that starting work no longer requires the orchestrator's own
command line. It reads the handshake the daemon wrote, presents the token, and calls the same
control routes the cockpit's buttons call.

    python -m orchestrator --daemon            # in one terminal

    python tools/daemon_client.py status
    python tools/daemon_client.py start "add a health endpoint and a test for it"
    python tools/daemon_client.py jobs
    python tools/daemon_client.py cancel <job id>
    python tools/daemon_client.py approvals
    python tools/daemon_client.py approve <approval id> --note "looks right"
    python tools/daemon_client.py deliver <card id>
    python tools/daemon_client.py watch                 # follow the live stream

It depends on nothing but the standard library, and on nothing in `orchestrator` either -
which is the point. Anything that can read a JSON file and make an HTTP request can drive
this daemon; the token is what makes that deliberate rather than ambient.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

TOKEN_HEADER = "X-Orchestrator-Token"


def load_handshake(project_root):
    """Read `.orchestrator/daemon.json`, or explain why there is nothing to talk to."""
    path = os.path.join(project_root, ".orchestrator", "daemon.json")
    if not os.path.isfile(path):
        sys.exit(
            "No daemon is running for %s.\n"
            "Start one with:  python -m orchestrator --daemon" % project_root
        )
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def request(handshake, path, payload=None, stream=False):
    url = "http://127.0.0.1:%d%s" % (int(handshake["port"]), path)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body)
    req.add_header(TOKEN_HEADER, handshake["token"])
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        response = urllib.request.urlopen(req, timeout=None if stream else 30)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except Exception:
            pass
        sys.exit("The daemon refused that (%d): %s" % (exc.code, detail))
    except urllib.error.URLError as exc:
        sys.exit("Could not reach the daemon: %s" % exc)
    if stream:
        return response
    return json.loads(response.read().decode("utf-8"))


def show(payload):
    print(json.dumps(payload, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", help="status, start, jobs, cancel, approvals, approve, "
                                        "reject, deliver, cockpit, watch")
    parser.add_argument("argument", nargs="?", default="", help="the goal, id, or card")
    parser.add_argument("--project-root", default=os.getcwd())
    parser.add_argument("--note", default="", help="with approve or reject")
    parser.add_argument("--parallel", type=int, default=None, help="with start")
    args = parser.parse_args()

    handshake = load_handshake(os.path.abspath(args.project_root))
    command = args.command

    if command == "status":
        show(request(handshake, "/api/daemon"))
    elif command == "jobs":
        show(request(handshake, "/api/jobs"))
    elif command == "cockpit":
        show(request(handshake, "/api/cockpit"))
    elif command == "approvals":
        show(request(handshake, "/api/cockpit")["approvals"])
    elif command == "start":
        if not args.argument:
            sys.exit('A goal is needed:  daemon_client.py start "..."')
        show(request(handshake, "/api/control/start",
                     {"goal": args.argument, "parallel": args.parallel}))
    elif command == "cancel":
        show(request(handshake, "/api/control/cancel", {"job_id": args.argument}))
    elif command in ("approve", "reject"):
        show(request(handshake, "/api/control/" + command,
                     {"id": args.argument, "note": args.note}))
    elif command == "deliver":
        show(request(handshake, "/api/control/deliver", {"card": args.argument}))
    elif command == "watch":
        stream = request(handshake, "/api/stream?since=-1", stream=True)
        print("Watching. Ctrl-C to stop.\n")
        event = None
        for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                data = line.split(":", 1)[1].strip()
                try:
                    parsed = json.loads(data)
                except Exception:
                    parsed = data
                name = parsed.get("event") if isinstance(parsed, dict) else ""
                print("%-14s %s" % (event, name or json.dumps(parsed, default=str)[:120]))
    else:
        sys.exit("Unknown command '%s'. Try --help." % command)


if __name__ == "__main__":
    main()
