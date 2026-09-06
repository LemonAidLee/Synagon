import os
import zipfile
import subprocess
import time
import urllib.request
import json

base_dir = r"D:\Progmata\Project Beta\tools\antigravity_terminal_bridge"
vsix_path = r"D:\Progmata\Project Beta\tools\antigravity_terminal_bridge.vsix"

content_types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="js" ContentType="application/javascript"/>
</Types>"""

vsix_manifest = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Id="antigravity-terminal-bridge" Version="1.0.0" Publisher="project-beta" TargetPlatform="universal"/>
    <DisplayName>Antigravity Integrated Terminal Bridge</DisplayName>
    <Description>Bridge extension allowing external orchestrators (LangGraph) to launch and control terminals inside Antigravity IDE</Description>
    <Categories>Other</Categories>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>"""

with zipfile.ZipFile(vsix_path, "w") as zf:
    zf.writestr("[Content_Types].xml", content_types)
    zf.writestr("extension.vsixmanifest", vsix_manifest)
    zf.write(os.path.join(base_dir, "package.json"), "extension/package.json")
    zf.write(os.path.join(base_dir, "extension.js"), "extension/extension.js")

print(f"Packed VSIX: {vsix_path}")

# Install extension
ide_cmd = r"C:\Users\Asus\AppData\Local\Programs\Antigravity IDE\bin\antigravity-ide.cmd"
res = subprocess.run([ide_cmd, "--install-extension", vsix_path, "--force"], capture_output=True, text=True)
print("Install exit code:", res.returncode)
print("Install stdout:", res.stdout)
print("Install stderr:", res.stderr)

# Also copy directory directly into .antigravity-ide/extensions as well to be certain
target_dir = r"C:\Users\Asus\.antigravity-ide\extensions\project-beta.antigravity-terminal-bridge-1.0.0"
os.makedirs(target_dir, exist_ok=True)
with open(os.path.join(base_dir, "package.json"), "r", encoding="utf-8") as f_in, open(os.path.join(target_dir, "package.json"), "w", encoding="utf-8") as f_out:
    f_out.write(f_in.read())
with open(os.path.join(base_dir, "extension.js"), "r", encoding="utf-8") as f_in, open(os.path.join(target_dir, "extension.js"), "w", encoding="utf-8") as f_out:
    f_out.write(f_in.read())

print("Direct copy complete.")
