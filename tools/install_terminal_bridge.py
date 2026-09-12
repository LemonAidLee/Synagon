#!/usr/bin/env python3
"""Utility script to package and install the Antigravity Integrated Terminal Bridge extension.

This script packages the bridge extension from `tools/antigravity_terminal_bridge`
into a VSIX package and installs it into the running Antigravity IDE using:
  `antigravity-ide.cmd --install-extension <vsix> --force`
It also copies the unpacked extension directly into `.antigravity-ide/extensions`
to ensure persistence across IDE sessions.
"""

import json
import os
import shutil
import subprocess
import sys
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTENSION_DIR = os.path.join(BASE_DIR, "antigravity_terminal_bridge")
VSIX_PATH = os.path.join(BASE_DIR, "antigravity_terminal_bridge.vsix")

CONTENT_TYPES_XML = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="js" ContentType="application/javascript"/>
</Types>"""

VSIX_MANIFEST_XML = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Id="antigravity-terminal-bridge" Version="1.0.0" Publisher="synagon" TargetPlatform="universal"/>
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


def build_vsix() -> str:
    """Pack extension files into a VSIX archive."""
    with zipfile.ZipFile(VSIX_PATH, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        zf.writestr("extension.vsixmanifest", VSIX_MANIFEST_XML)
        zf.write(os.path.join(EXTENSION_DIR, "package.json"), "extension/package.json")
        zf.write(os.path.join(EXTENSION_DIR, "extension.js"), "extension/extension.js")
    return VSIX_PATH


def install_extension(vsix_file: str) -> bool:
    """Install the VSIX into Antigravity IDE."""
    ide_cmd = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Antigravity IDE\bin\antigravity-ide.cmd")
    if not os.path.isfile(ide_cmd):
        ide_cmd = shutil.which("antigravity-ide.cmd") or shutil.which("antigravity-ide")

    if not ide_cmd or not os.path.isfile(ide_cmd):
        print("Warning: antigravity-ide.cmd not found on PATH or default locations.", file=sys.stderr)
        return False

    cmd = [ide_cmd, "--install-extension", vsix_file, "--force"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Failed to install extension: {res.stderr or res.stdout}", file=sys.stderr)
        return False

    # Also persist directly into .antigravity-ide/extensions
    user_exts = os.path.expanduser(r"~/.antigravity-ide/extensions/synagon.antigravity-terminal-bridge-1.0.0")
    os.makedirs(user_exts, exist_ok=True)
    shutil.copy2(os.path.join(EXTENSION_DIR, "package.json"), os.path.join(user_exts, "package.json"))
    shutil.copy2(os.path.join(EXTENSION_DIR, "extension.js"), os.path.join(user_exts, "extension.js"))

    return True


def main():
    print("Building Antigravity Terminal Bridge VSIX...")
    vsix = build_vsix()
    print(f"VSIX built: {vsix}")
    print("Installing extension into Antigravity IDE...")
    ok = install_extension(vsix)
    if ok:
        print("Antigravity Terminal Bridge installed successfully.")
    else:
        print("Installation had warnings or failed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
