import json
import os
import sys
import tempfile
import time
import urllib.request

tmp_dir = tempfile.mkdtemp(prefix="test_lg_term_")
job_file = os.path.join(tmp_dir, "job.json")
status_file = os.path.join(tmp_dir, "status.json")

job_spec = {
    "title": "LangGraph - Test Agent",
    "agent": "TestAgent",
    "role": "Researcher",
    "model": "test-model",
    "cmd": [sys.executable, "-c", "import sys; print('Line 1 from agent'); sys.stderr.write('Error line from agent\\n'); print('Line 2 from agent')"],
    "cwd": r"D:\Progmata\Project Beta",
    "status_file": status_file,
    "timeout": 30,
    "pause_seconds": 0.5
}

with open(job_file, "w", encoding="utf-8") as f:
    json.dump(job_spec, f, indent=2)

orchestrator_pkg_parent = r"D:\Progmata\Project Beta"
runner_cmd = f'& "{sys.executable}" -m orchestrator.launcher --job-file "{job_file}"'

bridge_data = json.dumps({
    "title": "LangGraph - Test Agent",
    "cwd": r"D:\Progmata\Project Beta",
    "command": runner_cmd,
    "env": {
        "PYTHONPATH": orchestrator_pkg_parent,
        "PYTHONIOENCODING": "utf-8"
    }
}).encode("utf-8")

req = urllib.request.Request(
    "http://127.0.0.1:49182/create_terminal",
    data=bridge_data,
    headers={"Content-Type": "application/json"},
    method="POST"
)
with urllib.request.urlopen(req) as resp:
    print("Bridge create response:", resp.read().decode())

deadline = time.time() + 30
status_data = None
while time.time() < deadline:
    if os.path.isfile(status_file):
        try:
            with open(status_file, "r", encoding="utf-8") as sf:
                status_data = json.load(sf)
            break
        except (json.JSONDecodeError, PermissionError):
            time.sleep(0.1)
    time.sleep(0.2)

print("Status data received:", status_data)

# Cleanup
import shutil
shutil.rmtree(tmp_dir, ignore_errors=True)
