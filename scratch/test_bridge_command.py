import urllib.request
import json
import os
import time

tmp_status = r"C:\Users\Asus\.antigravity-ide\test_bridge_runner_status.json"
if os.path.exists(tmp_status):
    os.remove(tmp_status)

# Create a small job to run in the terminal
cmd_to_send = f'python -c "import json; json.dump({{\'status\': \'ok\'}}, open(r\'{tmp_status}\', \'w\'))"'

data = json.dumps({
    "title": "Bridge Execution Test",
    "cwd": r"D:\Progmata\Project Beta",
    "command": cmd_to_send
}).encode("utf-8")

req = urllib.request.Request(
    "http://127.0.0.1:49182/create_terminal",
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST"
)

with urllib.request.urlopen(req) as resp:
    print("Bridge response:", resp.read().decode())

# Wait for file to be created by terminal
deadline = time.time() + 10
found = False
while time.time() < deadline:
    if os.path.exists(tmp_status):
        found = True
        break
    time.sleep(0.2)

print("File written by integrated terminal:", found)
if found:
    with open(tmp_status) as f:
        print("Content:", f.read())
    os.remove(tmp_status)

# Close test terminal
req_close = urllib.request.Request(
    "http://127.0.0.1:49182/close_terminal",
    data=json.dumps({"title": "Bridge Execution Test"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST"
)
with urllib.request.urlopen(req_close) as resp:
    print("Close response:", resp.read().decode())
