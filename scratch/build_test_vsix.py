import zipfile
import os

content_types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="js" ContentType="application/javascript"/>
</Types>"""

vsix_manifest = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Id="test-bridge" Version="1.0.0" Publisher="project-beta" TargetPlatform="universal"/>
    <DisplayName>Test Bridge</DisplayName>
    <Description>Test Bridge</Description>
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

vsix_path = r"D:\Progmata\Project Beta\test-bridge.vsix"
with zipfile.ZipFile(vsix_path, "w") as zf:
    zf.writestr("[Content_Types].xml", content_types)
    zf.writestr("extension.vsixmanifest", vsix_manifest)
    ext_dir = r"C:\Users\Asus\.antigravity-ide\extensions\project-beta.test-bridge-1.0.0"
    zf.write(os.path.join(ext_dir, "package.json"), "extension/package.json")
    zf.write(os.path.join(ext_dir, "extension.js"), "extension/extension.js")

print(f"VSIX created at {vsix_path}")
