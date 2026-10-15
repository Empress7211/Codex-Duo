#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$PWD/Codex Duo.app"
ORBIT_BUILD_RUNTIME=""
DUO_PYTHON_RUNTIME=""
DUO_SIGN_IDENTITY="-"
DUO_SOURCE_APP="/Applications/Codex.app"
if [ ! -d "$DUO_SOURCE_APP" ]; then DUO_SOURCE_APP="/Applications/ChatGPT.app"; fi
while [ "$#" -gt 0 ]; do
  case "$1" in
    --runtime-root) ORBIT_BUILD_RUNTIME="${2:?Missing runtime-root}" ;;
    --python-runtime) DUO_PYTHON_RUNTIME="${2:?Missing python-runtime}" ;;
    --sign-identity) DUO_SIGN_IDENTITY="${2:?Missing sign-identity}" ;;
    --source-app) DUO_SOURCE_APP="${2:?Missing source-app}" ;;
    --help|-h) echo 'Usage: ./build.sh --python-runtime /portable/python [--source-app /Applications/Codex.app] [--runtime-root /absolute/path] [--sign-identity ID]'; exit 0 ;;
    *) echo 'Unknown option. Run ./build.sh --help' >&2; exit 2 ;;
  esac
  shift 2
done
if [ -z "$DUO_PYTHON_RUNTIME" ]; then
  echo 'Provide --python-runtime pointing to a portable Python 3.11+ distribution.' >&2
  exit 2
fi
DUO_BUILD_PYTHON="$DUO_PYTHON_RUNTIME/bin/python3"
if [ ! -f assets/Orbit.icns ] || [ ! -f assets/Orbit.png ]; then
  echo 'Local icon assets are missing. Follow assets/README.md before building.' >&2
  exit 2
fi
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources/source"
BUILD_CACHE="${TMPDIR:-/tmp}/codex-dual-swift-cache"
mkdir -p "$BUILD_CACHE"
/usr/bin/swiftc -O -module-cache-path "$BUILD_CACHE" source/App.swift \
  -o "$APP_DIR/Contents/MacOS/CodexDuo" -framework AppKit
/usr/bin/swiftc -O -module-cache-path "$BUILD_CACHE" source/InstanceLauncher.swift \
  -o source/duo-instance-launcher -framework AppKit
cp source/backend.py source/ccswitch_reader.py source/history_store.py source/sidebar_store.py source/shared_store.py source/sidebar_sync.py source/runtime_sync.py source/managed_desktop.py source/local_signing.py source/managed_sync.js source/orbit_bridge.py source/orbit-cli "$APP_DIR/Contents/Resources/source/"
cp source/duo-instance-launcher "$APP_DIR/Contents/Resources/source/"
rm -f "$APP_DIR/Contents/Resources/source/duo-launch.cjs"
cp assets/Orbit.icns assets/Orbit.png "$APP_DIR/Contents/Resources/"
chmod +x "$APP_DIR/Contents/Resources/source/orbit-cli"
rm -rf "$APP_DIR/Contents/Resources/source/__pycache__"
"$DUO_BUILD_PYTHON" -B - "$APP_DIR" "$ORBIT_BUILD_RUNTIME" <<'PY'
import plistlib, sys
from pathlib import Path
app = Path(sys.argv[1])
location = app / 'Contents/Resources/runtime-location.json'
if sys.argv[2]:
    import json
    runtime = Path(sys.argv[2]).expanduser()
    if not runtime.is_absolute():
        raise SystemExit('runtime-root must be absolute')
    location.write_text(json.dumps({'root': str(runtime.resolve())}) + '\n')
elif location.exists():
    location.unlink()
with (app / 'Contents/Info.plist').open('wb') as out:
    plistlib.dump({
        'CFBundleName': 'Codex Duo',
        'CFBundleDisplayName': 'Codex Duo',
        'CFBundleIdentifier': 'local.codex-dual.launcher',
        'CFBundleExecutable': 'CodexDuo',
        'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': '0.6.0',
        'CFBundleVersion': '18',
        'CFBundleIconFile': 'Orbit.icns',
        'NSDocumentsFolderUsageDescription': '仅在你打开“文稿”中的项目时，Codex Duo 才需要访问该项目文件。实例登录数据保存在应用支持目录。',
        'LSMinimumSystemVersion': '14.0',
        'NSHighResolutionCapable': True,
        'NSPrincipalClass': 'NSApplication',
    }, out)
PY
"$DUO_BUILD_PYTHON" -B source/package_app.py --app "$APP_DIR" \
  --python-runtime "$DUO_PYTHON_RUNTIME" --source-app "$DUO_SOURCE_APP" --sign-identity "$DUO_SIGN_IDENTITY"
echo 'Built Codex Duo.app (self-contained test package; publisher signing is build-only)'
