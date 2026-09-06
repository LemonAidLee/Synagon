import urllib.request
import json
import os
import time

tmp_out = r"C:\Users\Asus\.antigravity-ide\shell_detect.txt"
if os.path.exists(tmp_out):
    os.remove(tmp_out)

# Command that works in both or detects shell
# In PowerShell: $env:PSModulePath is set
# Let's test running python directly
import sys
py = sys.executable
cmd = f'python -c "import sys, os; open(r\'{tmp_out}\', \'w\').write(sys.executable)"'

data = json.dumps({
    "title": "Shell Detect",
    "cwd": r"D:\Progmata\Project Beta",
    "command": cmd
}).encode("utf-8")

req = urllib.request.Request(
    "http://127.0.0.1:49182/create_terminal",
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST"
)
with urllib.request.urlopen(req) as resp:
    print("Created terminal:", resp.read().decode())

deadline = time.time() + 10
while time.time() < deadline:
    if os.path.exists(tmp_out):
        break
    time.sleep(0.2)

if os.path.exists(tmp_out):
    with open(tmp_out) as f:
        print("Written by terminal:", f.read())
    os.remove(tmp_out)

# Close
req_close = urllib.request.Request(
    "http://127.0.0.1:49182/close_terminal",
    data=json.dumps({"title": "Shell Detect"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST"
)
with urllib.request.urlopen(req_close) as resp:
    print("Closed:", resp.read().decode())
