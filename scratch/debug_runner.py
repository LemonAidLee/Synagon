import json
import urllib.request

job_file = r"C:\Users\Asus\AppData\Local\Temp\langgraph_terminal_cr3ug_vn\job.json"
cmd = f'& "D:\\Progmata\\Project Beta\\.venv\\Scripts\\python.exe" -m orchestrator.launcher --job-file "{job_file}"'
data = json.dumps({
    "title": "Test Execution Debug",
    "cwd": r"D:\Progmata\Integrated Terminal Validation",
    "command": cmd,
    "env": {"PYTHONPATH": r"D:\Progmata\Project Beta"}
}).encode("utf-8")

req = urllib.request.Request(
    "http://127.0.0.1:49182/create_terminal",
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST"
)
with urllib.request.urlopen(req) as resp:
    print("Resp:", resp.read().decode())
