"""Build managed copies from the locally installed official Codex.

The installed application is a read-only input. The generated copy lives under
an Orbit-owned root; replacing it never restarts a process. The bootstrap uses
the existing desktop state and query invalidation APIs, not the original IPC.
"""
from __future__ import annotations

import hashlib
import json
import mmap
import os
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid

from local_signing import sign_app


BUNDLE_ID = "local.codex-orbit.desktop"
APP_NAME = "Codex Duo Desktop"
DISPLAY_NAME = "Codex Duo Instance"
APP_BUNDLE_NAME = DISPLAY_NAME + ".app"
LEGACY_APP_BUNDLE_NAME = "Codex Orbit Instance.app"
PATCH_VERSION = 24
INTEGRITY_SENTINEL = b"AGbevlPCksUGKNL8TSn7wGmJEuJsXb2A"
KEYCHAIN_SERVICE_SUFFIX = b" Storage Key\0"
DUO_KEYCHAIN_SERVICE_SUFFIX = b" Duo Vault\0\0\0"
PATCH_RECEIPT = "duo-patches.json"


class DesktopCopyError(RuntimeError):
    pass


class DesktopCopyNeedsUpdate(DesktopCopyError):
    """The managed desktop is valid, but its source or integration patch is out of date."""


def managed_app_path(root: Path) -> Path:
    """Keep locating the installed old copy until a stopped upgrade replaces it."""
    desktop = Path(root) / "desktop"
    current = desktop / APP_BUNDLE_NAME
    legacy = desktop / LEGACY_APP_BUNDLE_NAME
    return legacy if not current.exists() and legacy.exists() else current


def validate_display_name(name: str) -> str:
    name = name.strip()
    if (not name or len(name) > 80 or len(name.encode('utf-8')) > 240
            or any(c in '/:' or ord(c) < 32 for c in name)):
        raise DesktopCopyError("实例名称需为 1 到 80 个字符，不含斜杠、冒号或控制字符，文件名长度不超过 240 字节。")
    return name


def profile_bundle_id(profile_id: str) -> str:
    return BUNDLE_ID + "." + profile_id


def profile_app_path(root: Path, profile: dict) -> Path:
    directory = Path(root) / 'profiles' / profile['id'] / 'desktop'
    record = directory / 'copy.json'
    # Legacy labels were unrestricted; status/stop/rename must remain usable
    # before a user repairs a label that cannot become an exact Dock filename.
    filename = profile['id'] + '.app'
    if record.exists():
        filename = json.loads(record.read_text())['app_name']
        if Path(filename).name != filename or not filename.endswith('.app'):
            raise DesktopCopyError("实例应用包记录的路径无效。")
    else:
        try:
            filename = validate_display_name(profile['name']) + '.app'
        except DesktopCopyError:
            pass
    return directory / filename


def _owned_root(root: Path) -> Path:
    root = Path(root).expanduser().resolve()
    try:
        manifest = json.loads((root / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise DesktopCopyError("缺少 Orbit 所有权清单，未生成桌面副本。") from exc
    if manifest.get("owner") != "codex-dual" or Path(manifest.get("root", "")).resolve() != root:
        raise DesktopCopyError("桌面副本目标不属于 Orbit，未写入。")
    desktop = root / "desktop"
    if desktop.is_symlink():
        raise DesktopCopyError("桌面副本目录不能是符号链接。")
    desktop.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def read_asar_header(path: Path) -> tuple[dict, int, bytes]:
    with Path(path).open("rb") as stream:
        prefix = stream.read(16)
        if len(prefix) != 16:
            raise DesktopCopyError("Codex app.asar 头不完整。")
        size, header_size, payload_size, string_size = struct.unpack("<4I", prefix)
        if size != 4 or header_size != payload_size + 4 or string_size > payload_size - 4:
            raise DesktopCopyError("Codex app.asar 头格式不受支持。")
        header_json = stream.read(string_size)
    return json.loads(header_json), 8 + header_size, header_json


def _entry(header: dict, name: str) -> dict:
    entry = header
    for component in name.split("/"):
        entry = entry["files"][component]
    return entry


def read_asar_file(path: Path, name: str) -> bytes:
    header, start, _ = read_asar_header(path)
    entry = _entry(header, name)
    if entry.get("unpacked") or "offset" not in entry:
        raise DesktopCopyError("目标脚本不是打包的 asar 文件。")
    with Path(path).open("rb") as stream:
        stream.seek(start + int(entry["offset"]))
        body = stream.read(entry["size"])
    if len(body) != entry["size"]:
        raise DesktopCopyError("Codex app.asar 脚本不完整。")
    return body


def framework_path(app: Path) -> Path:
    """Resolve Electron's installed framework without pinning its version."""
    frameworks = list((app / 'Contents/Frameworks').glob('*.framework'))
    matches = [p / 'Versions/Current' / p.stem for p in frameworks
               if (p / 'Versions/Current' / p.stem).is_file()]
    matches = [p for p in matches if 'Codex' in p.name or 'Electron' in p.name]
    if len(matches) != 1 or not matches[0].resolve().is_relative_to(app.resolve()):
        raise DesktopCopyError('无法唯一定位 Codex 的 Electron framework，未修改副本。')
    return matches[0]


def source_identity(app: Path) -> dict:
    """Use native metadata and the archive's own integrity receipt for reuse."""
    app = Path(app).expanduser().resolve()
    try:
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    except FileNotFoundError:
        raise DesktopCopyError('未找到本机原生 Codex，请先安装官方桌面应用。') from None
    executable = info.get('CFBundleExecutable', '')
    if (info.get('CFBundleIdentifier') != 'com.openai.codex' or
            not executable or Path(executable).name != executable or
            not (app / 'Contents/MacOS' / executable).is_file()):
        raise DesktopCopyError('所选应用不是可识别的原生 Codex。')
    _, _, header_json = read_asar_header(app / 'Contents/Resources/app.asar')
    header_hash = hashlib.sha256(header_json).hexdigest()
    if info.get('ElectronAsarIntegrity', {}).get('Resources/app.asar') != {
            'algorithm': 'SHA256', 'hash': header_hash}:
        raise DesktopCopyError('原生 Codex 的应用归档与完整性记录不一致，请等待更新完成后重试。')
    return {'build': info['CFBundleVersion'], 'version': info['CFBundleShortVersionString'],
            'executable': executable, 'source_header_sha256': header_hash}


def _asar_entries(header: dict, prefix: str = ''):
    for name, entry in header.get('files', {}).items():
        path = prefix + name
        if 'files' in entry:
            yield from _asar_entries(entry, path + '/')
        else:
            yield path, entry


def _is_numeric_renderer_candidate(body: bytes) -> bool:
    """Keep only chunks containing a complete renderer integration contract."""
    contracts = (
        (b'recordThreadRuntimeStatusEvidence', b'.threadStore.conversations.has', b'thread/status/changed'),
        (b'workspace-root-options-updated', b'global-state-updated', b'switch('),
        (b'isWriterConflict:', b'retryResume:', b'resumeLiveWriterError'),
        (b'isWriterConflict:', b'submitDisabled:', b'isResuming:', b'isThreadHistoryLoading:'),
    )
    from desktop_speed_ui_patches import is_speed_ui_script
    speed_request = (b'Failed to read service tier for request' in body and
                     b'Failed to read service tier model' in body and b'amazonBedrock' in body)
    return (any(all(marker in body for marker in contract) for contract in contracts)
            or is_speed_ui_script(body) or speed_request)


def read_patch_scripts(asar: Path, names=None) -> dict[str, bytes]:
    # Read the header once. Locale and unrelated feature chunks account for
    # hundreds of MB, so inspect only main-process and relevant UI modules.
    header, start, _ = read_asar_header(asar)
    prefixes = ('app-shared-', 'app-initial-', 'local-conversation-thread-',
                'use-resume-conversation-if-needed-', 'initialize-app-host-services-')
    selected = set(names) if names is not None else None
    result = {}
    with asar.open('rb') as stream:
        if selected is None:
            package_entry = _entry(header, 'package.json')
            stream.seek(start + int(package_entry['offset']))
            package = json.loads(stream.read(package_entry['size']))
            main = package['main'].removeprefix('./')
            main_directory = str(Path(main).parent) + '/'
        for name, entry in _asar_entries(header):
            wanted = (name in selected if selected is not None else
                      name == 'package.json' or
                      name.startswith(main_directory) and name.endswith('.js') or
                      name.endswith('.js') and
                      Path(name).name.startswith(prefixes))
            numeric_chunk = (selected is None and name.startswith('webview/assets/async/')
                             and re.fullmatch(r'\d+\.[0-9a-f]+\.js', Path(name).name))
            if not wanted and not numeric_chunk:
                continue
            if entry.get('unpacked') or 'offset' not in entry:
                if wanted:
                    raise DesktopCopyError('Codex 集成脚本不是完整的归档文件：' + name)
                continue
            stream.seek(start + int(entry['offset']))
            body = stream.read(entry['size'])
            if len(body) != entry['size']:
                raise DesktopCopyError('Codex 集成脚本不完整：' + name)
            if wanted or _is_numeric_renderer_candidate(body):
                result[name] = body
    if selected is not None and result.keys() != selected:
        raise DesktopCopyError('受管归档缺少已记录的集成脚本。')
    return result


def patch_plan(app: Path) -> dict[str, bytes]:
    from desktop_main_patches import discover_main_patches, PatchError as MainPatchError
    from desktop_renderer_patches import discover_renderer_patches, PatchError as RendererPatchError
    scripts = read_patch_scripts(app / 'Contents/Resources/app.asar')
    if not any(b'CODEX_ELECTRON_USER_DATA_PATH' in body and b'userData' in body
               for body in scripts.values()) or not any(b'CODEX_HOME' in b for b in scripts.values()):
        raise DesktopCopyError('当前原生 Codex 未提供可识别的独立登录/窗口目录入口。')
    try:
        main = discover_main_patches(scripts, APP_NAME, DISPLAY_NAME)
        renderer = discover_renderer_patches(scripts)
    except (MainPatchError, RendererPatchError) as error:
        raise DesktopCopyError('当前 Codex 的集成接口不兼容：' + str(error)) from error
    # Speed support is optional: if the official UI changes, preserve the
    # working Duo copy without installing a partial or misleading control.
    from desktop_speed_request_patches import discover_speed_request_patches, PatchError as SpeedRequestError
    from desktop_speed_ui_patches import discover_speed_ui_patches
    base = {**main, **renderer}
    try:
        working = {**scripts, **base}
        request = discover_speed_request_patches(working)
        working.update(request)
        ui = discover_speed_ui_patches(working)
    except (SpeedRequestError, RendererPatchError) as error:
        print('Codex Duo API 速度选择不可用：' + str(error), file=sys.stderr)
        return base
    return {**base, **request, **ui}


def check_desktop_support(app: Path) -> dict:
    """Read-only capability check, independent of the native build number."""
    app = Path(app).expanduser().resolve()
    identity = source_identity(app)
    patches = patch_plan(app)
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if framework_integrity_digest(framework_path(app))[1] != integrity_dictionary_digest(info['ElectronAsarIntegrity']):
        raise DesktopCopyError('原生 Codex 的 Electron 完整性记录不一致。')
    return {**identity, 'patch_scripts': len(patches),
            'api_speed_available': any(b'orbit-api-speed-request-hook' in body for body in patches.values())}


def _copy_signature(source_app: Path, sync_bytes: bytes) -> dict:
    return {**source_identity(source_app), 'source_app': str(source_app.resolve()),
            'patch_version': PATCH_VERSION,
            'launcher_sha256': hashlib.sha256(Path(__file__).with_name('duo-instance-launcher').read_bytes()).hexdigest(),
            'sync_sha256': hashlib.sha256(sync_bytes).hexdigest()}


def _ipc_path(root: Path, profile: str) -> Path:
    tag = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    profile_tag = hashlib.sha256(profile.encode()).hexdigest()[:12]
    return Path('/private/tmp') / ('codex-orbit-' + str(os.getuid())) / tag / profile_tag


def ensure_ipc_dir(root: Path, profile: str) -> Path:
    """Keep each managed bus below macOS's 104-byte Unix socket path limit."""
    root = _owned_root(root)
    manifest = json.loads((root / "manifest.json").read_text())
    if (not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", profile) or
            profile not in {item["id"] for item in manifest["profiles"]}):
        raise DesktopCopyError("IPC 目录只能用于清单中的受管实例。")
    directory = _ipc_path(root, profile)
    base = directory.parent.parent
    for current in (base, directory.parent, directory):
        if current.is_symlink():
            raise DesktopCopyError("受管 IPC 路径不能是符号链接。")
        current.mkdir(mode=0o700, exist_ok=True)
        if current.stat().st_uid != os.getuid():
            raise DesktopCopyError("受管 IPC 目录不属于当前用户。")
        current.chmod(0o700)
    marker = directory / ".orbit-owner.json"
    identity = {"owner": "codex-dual", "root": str(root), "profile": profile}
    if marker.is_symlink():
        raise DesktopCopyError("受管 IPC 所有权文件不能是符号链接。")
    if marker.exists():
        if json.loads(marker.read_text()) != identity:
            raise DesktopCopyError("受管 IPC 目录所有权不匹配。")
    else:
        marker.write_text(json.dumps(identity))
        marker.chmod(0o600)
    return directory


def replace_asar_file(path: Path, name: str, body: bytes) -> str:
    return replace_asar_files(path, {name: body})


def replace_asar_files(path: Path, replacements: dict[str, bytes]) -> str:
    """Append one replacement blob and rebuild only the small archive header.

    Existing offsets, aliases, symlinks and unpacked entries remain unchanged.
    Stream-copy the data section once; never extract the full archive to disk.
    Returns the exact header hash required by ElectronAsarIntegrity.
    """
    path = Path(path)
    if path.is_symlink():
        raise DesktopCopyError("拒绝修改符号链接 app.asar。")
    header, old_start, _ = read_asar_header(path)
    payload_size = path.stat().st_size - old_start
    offset = payload_size
    for name, body in replacements.items():
        entry = _entry(header, name)
        entry.update(size=len(body), offset=str(offset))
        offset += len(body)
        block_size = entry.get("integrity", {}).get("blockSize", 4194304)
        entry["integrity"] = {
            "algorithm": "SHA256", "hash": hashlib.sha256(body).hexdigest(),
            "blockSize": block_size,
            "blocks": [hashlib.sha256(body[i:i + block_size]).hexdigest()
                       for i in range(0, len(body), block_size)],
        }
    header_json = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    string_pickle = struct.pack("<I", len(header_json)) + header_json
    string_pickle += b"\0" * (-len(string_pickle) % 4)
    header_pickle = struct.pack("<I", len(string_pickle)) + string_pickle
    prefix = struct.pack("<II", 4, len(header_pickle))
    descriptor, temporary = tempfile.mkstemp(prefix=".orbit-asar-", dir=path.parent)
    try:
        os.fchmod(descriptor, path.stat().st_mode & 0o777)
        with os.fdopen(descriptor, "wb") as out, path.open("rb") as source:
            out.write(prefix)
            out.write(header_pickle)
            source.seek(old_start)
            shutil.copyfileobj(source, out, 1024 * 1024)
            for body in replacements.values():
                out.write(body)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(header_json).hexdigest()


def _run(command: list[str], *, timeout=120) -> None:
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        # Subprocess diagnostics may contain local paths or other sensitive data.
        operation = '签名验证' if command[0] == '/usr/bin/codesign' else '复制'
        raise DesktopCopyError('桌面副本' + operation + '失败。')


def integrity_dictionary_digest(integrity: dict) -> bytes:
    # Electron 41 additionally validates the Info.plist dictionary against a
    # 32-byte digest in its framework, before validating the asar header. Merely
    # updating the plist and re-signing causes a real launch-time fatal error.
    text = "".join(key + integrity[key]["algorithm"] + integrity[key]["hash"]
                   for key in sorted(integrity))
    return hashlib.sha256(text.encode("utf-8")).digest()


def framework_integrity_digest(path: Path) -> tuple[int, bytes]:
    with Path(path).open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
        offset = mapped.find(INTEGRITY_SENTINEL)
        if offset < 0 or mapped.find(INTEGRITY_SENTINEL, offset + 1) >= 0:
            raise DesktopCopyError("Electron asar 字典摘要槽与已核验版本不同。")
        if mapped[offset + 32:offset + 34] != b"\x01\x01":
            raise DesktopCopyError("Electron asar 字典摘要版本不受支持。")
        return offset + 34, bytes(mapped[offset + 34:offset + 66])


def update_framework_integrity(path: Path, before: dict, after: dict) -> None:
    offset, old_digest = framework_integrity_digest(path)
    if old_digest != integrity_dictionary_digest(before):
        raise DesktopCopyError("Electron 原字典摘要校验失败，未修改副本。")
    with Path(path).open("r+b") as stream:
        stream.seek(offset)
        stream.write(integrity_dictionary_digest(after))
        stream.flush()
        os.fsync(stream.fileno())


def patch_keychain_service(path: Path) -> None:
    """Give the managed framework a keychain service distinct from Codex."""
    if len(KEYCHAIN_SERVICE_SUFFIX) != len(DUO_KEYCHAIN_SERVICE_SUFFIX):
        raise DesktopCopyError("受管钥匙串服务名称长度不匹配。")
    with Path(path).open("r+b") as stream, mmap.mmap(stream.fileno(), 0,
                                                      access=mmap.ACCESS_READ) as mapped:
        offset = mapped.find(KEYCHAIN_SERVICE_SUFFIX)
        if (offset < 0 or mapped.find(KEYCHAIN_SERVICE_SUFFIX, offset + 1) >= 0
                or mapped.find(DUO_KEYCHAIN_SERVICE_SUFFIX) >= 0):
            raise DesktopCopyError("Electron 钥匙串服务名称位置与已核验版本不同。")
        stream.seek(offset)
        stream.write(DUO_KEYCHAIN_SERVICE_SUFFIX)
        stream.flush()
        os.fsync(stream.fileno())


def verify_copy(app: Path, *, allow_legacy_display: bool = False,
                profile_id: str | None = None, display_name: str | None = None) -> dict:
    app = Path(app)
    resources = app / 'Contents/Resources'
    plist = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    asar = resources / 'app.asar'
    header, _, header_json = read_asar_header(asar)
    header_hash = hashlib.sha256(header_json).hexdigest()
    if plist.get('ElectronAsarIntegrity', {}).get('Resources/app.asar') != {
            'algorithm': 'SHA256', 'hash': header_hash}:
        raise DesktopCopyError('受管 app.asar 与 Electron 头校验不一致。')
    try:
        receipt = json.loads((resources / PATCH_RECEIPT).read_text())
    except FileNotFoundError:
        raise DesktopCopyNeedsUpdate('实例使用旧版集成方式，需要在停止后从原生 Codex 更新。') from None
    if receipt.get('patch_version') != PATCH_VERSION:
        raise DesktopCopyNeedsUpdate('实例集成需要更新，请先关闭对应实例。')
    scripts = read_patch_scripts(asar, receipt['scripts'])
    for name, body in scripts.items():
        digest = hashlib.sha256(body).hexdigest()
        if digest != receipt['scripts'][name] or digest != _entry(header, name)['integrity']['hash']:
            raise DesktopCopyError('受管集成脚本与生成记录不一致：' + name)
    # The receipt is sealed by the app signature. Verify the actual integration
    # markers as well, without depending on hashed filenames or minified names.
    from desktop_main_patches import validate_main_patches, PatchError
    try:
        validate_main_patches(scripts, APP_NAME, DISPLAY_NAME)
    except PatchError as error:
        raise DesktopCopyError('受管主进程集成校验失败：' + str(error)) from error
    for marker in (b'orbit-renderer-writer-release', b'orbit-renderer-host-events',
                   b'orbit-renderer-resume', b'orbit-renderer-input-gates'):
        if not any(marker in body for body in scripts.values()):
            raise DesktopCopyError('受管会话集成标记不完整。')
    expected_id = profile_bundle_id(profile_id) if profile_id else BUNDLE_ID
    if plist.get('CFBundleIdentifier') != expected_id:
        raise DesktopCopyError('受管桌面副本没有独立应用标识。')
    if (plist.get('CFBundleName') != (display_name or DISPLAY_NAME) or
            plist.get('CFBundleDisplayName') != plist.get('CFBundleName')):
        raise DesktopCopyError('受管桌面副本没有独立应用名称。')
    framework = framework_path(app)
    if framework_integrity_digest(framework)[1] != integrity_dictionary_digest(plist['ElectronAsarIntegrity']):
        raise DesktopCopyError('Electron 内嵌字典摘要与受管 app.asar 不一致。')
    with framework.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
        if mapped.find(DUO_KEYCHAIN_SERVICE_SUFFIX) < 0 or mapped.find(KEYCHAIN_SERVICE_SUFFIX) >= 0:
            raise DesktopCopyError('受管桌面仍会访问官方存储钥匙串。')
    if profile_id:
        marker = json.loads((resources / 'duo-instance.json').read_text())
        if marker['profile_id'] != profile_id:
            raise DesktopCopyError('实例应用包的启动身份不匹配。')
    if plist.get('CFBundleExecutable') != 'DuoInstance':
        raise DesktopCopyError('受管应用没有原生隔离启动入口。')
    engine = app / 'Contents/MacOS' / receipt['executable']
    for code in (engine, app):
        _run(['/usr/bin/codesign', '--verify', '-R', '=identifier "' + BUNDLE_ID + '"', str(code)])
    _run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)])
    return {'build': plist['CFBundleVersion'], 'header_sha256': header_hash,
            'signature_verified': True}


def _install_copy(staging: Path, destination: Path, existing: Path,
                  record_path: Path, record: dict) -> None:
    if destination != existing and destination.exists() and not destination.samefile(existing):
        raise DesktopCopyError('新名称的应用路径已有文件，未覆盖。')
    previous = None
    installed = False
    saved_previous = json.loads(record_path.read_text()).get('previous_app') if record_path.exists() else None
    temporary_record = record_path.with_name('.copy-' + uuid.uuid4().hex + '.tmp')
    if existing.exists():
        previous = existing.with_name('.previous-' + uuid.uuid4().hex + '.app')
        existing.rename(previous)
    try:
        staging.rename(destination)
        installed = True
        temporary_record.write_text(json.dumps({**record, 'previous_app': saved_previous}, indent=2) + '\n')
        os.replace(temporary_record, record_path)
    except BaseException:
        if installed:
            shutil.rmtree(destination)
        if previous:
            previous.rename(existing)
        raise
    finally:
        temporary_record.unlink(missing_ok=True)
    if previous:
        shutil.rmtree(previous)
    _cleanup_recorded_previous(record_path)


def _cleanup_recorded_previous(record_path: Path) -> None:
    record = json.loads(record_path.read_text())
    if not record.get('previous_app'):
        return
    previous = Path(record['previous_app'])
    if (previous.parent.resolve() != record_path.parent.resolve() or previous.is_symlink()
            or not previous.name.startswith('.previous-') or previous.suffix != '.app'):
        raise DesktopCopyError('旧应用备份的所有权路径不匹配，未清理。')
    if previous.exists():
        info = plistlib.loads((previous / 'Contents/Info.plist').read_bytes())
        if info['CFBundleIdentifier'] != record.get('bundle_id', BUNDLE_ID):
            raise DesktopCopyError('旧应用备份的身份不匹配，未清理。')
        shutil.rmtree(previous)
    record['previous_app'] = None
    temporary = record_path.with_name('.copy-' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(record, indent=2) + '\n')
        os.replace(temporary, record_path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_copy(root: Path, source_app: Path, sync_module: Path | None = None,
                 *, allow_replace_stopped: bool = False, signing_identity: str = "-") -> Path:
    """Clone the installed native app and apply discovered integration seams."""
    root = _owned_root(root)
    source_app = Path(source_app).expanduser().resolve()
    if source_app.is_relative_to(root):
        raise DesktopCopyError('原生 Codex 输入不得来自 Duo 生成目录。')
    destination = root / 'desktop' / APP_BUNDLE_NAME
    existing = managed_app_path(root)
    sync_bytes = Path(sync_module or Path(__file__).with_name('managed_sync.js')).read_bytes()
    signature = _copy_signature(source_app, sync_bytes)
    record_path = root / 'desktop/copy.json'
    if existing.exists():
        if not record_path.exists():
            raise DesktopCopyError('受管副本已有文件但无完成记录，未覆盖。')
        saved = json.loads(record_path.read_text())
        if existing == destination and all(saved.get(k) == v for k, v in signature.items()):
            verify_copy(destination)
            _cleanup_recorded_previous(record_path)
            return destination
        if not allow_replace_stopped:
            raise DesktopCopyNeedsUpdate('原生 Codex 或 Duo 集成已更新；请先关闭对应受管实例。')
    staging = root / 'desktop' / ('.preparing-' + uuid.uuid4().hex + '.app')
    try:
        # A local APFS clone snapshots the installed official app. Never reuse
        # an engine bundled in the manager or mutate the original installation.
        _run(['/bin/cp', '-cR', str(source_app), str(staging)])
        if source_identity(staging) != {k: signature[k] for k in
                ('build', 'version', 'executable', 'source_header_sha256')}:
            raise DesktopCopyError('准备期间原生 Codex 发生更新，请待更新完成后重试。')
        patches = patch_plan(staging)
        resources = staging / 'Contents/Resources'
        info_path = staging / 'Contents/Info.plist'
        source_plist = plistlib.loads(info_path.read_bytes())
        (resources / 'orbit-sync.cjs').write_bytes(sync_bytes)
        launcher = staging / 'Contents/MacOS/DuoInstance'
        shutil.copyfile(Path(__file__).with_name('duo-instance-launcher'), launcher)
        launcher.chmod(0o755)
        receipt = {'patch_version': PATCH_VERSION, 'executable': signature['executable'],
                   'api_speed_available': any(b'orbit-api-speed-request-hook' in body for body in patches.values()),
                   'source': {k: signature[k] for k in ('build', 'version', 'source_header_sha256')},
                   'scripts': {name: hashlib.sha256(body).hexdigest() for name, body in patches.items()}}
        (resources / PATCH_RECEIPT).write_text(json.dumps(receipt, indent=2) + '\n')
        header_hash = replace_asar_files(resources / 'app.asar', patches)
        copy_plist = dict(source_plist)
        copy_plist.update(CFBundleName=DISPLAY_NAME, CFBundleDisplayName=DISPLAY_NAME,
                          CFBundleIdentifier=BUNDLE_ID, CFBundleExecutable='DuoInstance',
                          NSDocumentsFolderUsageDescription='仅在打开“文稿”中的项目时，此 Codex 窗口才需要访问该项目文件。')
        copy_plist.pop('CFBundleURLTypes', None)
        copy_plist['ElectronAsarIntegrity'] = {
            **copy_plist.get('ElectronAsarIntegrity', {}),
            'Resources/app.asar': {'algorithm': 'SHA256', 'hash': header_hash}}
        framework = framework_path(staging)
        update_framework_integrity(framework, source_plist['ElectronAsarIntegrity'],
                                   copy_plist['ElectronAsarIntegrity'])
        patch_keychain_service(framework)
        info_path.write_bytes(plistlib.dumps(copy_plist))
        sign_app(staging, identity=signing_identity)
        sign_app(staging / 'Contents/MacOS' / signature['executable'], deep=False,
                 identifier=BUNDLE_ID, identity=signing_identity)
        sign_app(staging, deep=False, identifier=BUNDLE_ID, identity=signing_identity)
        verified = verify_copy(staging)
        _install_copy(staging, destination, existing, record_path, {**signature, **verified})
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def ensure_desktop(root: Path, source_app: Path = Path("/Applications/ChatGPT.app"),
                   *, allow_replace_stopped: bool = False) -> Path:
    """The caller checks that no old process runs directly from the template."""
    return prepare_copy(root, source_app, allow_replace_stopped=allow_replace_stopped)


def ensure_profile_desktop(root: Path, profile: dict,
                           source_app: Path = Path('/Applications/ChatGPT.app'), *,
                           allow_replace_stopped: bool = False) -> Path:
    root = _owned_root(root)
    name = validate_display_name(profile['name'])
    base = root / 'profiles' / profile['id']
    directory = base / 'desktop'
    if (not base.is_dir() or base.is_symlink() or directory.is_symlink()
            or directory.resolve() != directory):
        raise DesktopCopyError('实例应用目录无效，未写入。')
    directory.mkdir(mode=0o700, exist_ok=True)
    record_path = directory / 'copy.json'
    existing = profile_app_path(root, profile)
    if existing.is_symlink() or record_path.is_symlink():
        raise DesktopCopyError('实例应用包或完成记录不能是符号链接。')
    destination = directory / (name + '.app')
    sync_bytes = Path(__file__).with_name('managed_sync.js').read_bytes()
    manifest = json.loads((root / 'manifest.json').read_text())
    marker = {'profile_id': profile['id'], 'python': sys.executable,
              'source_home': manifest.get('sync', {}).get('source_home', str(Path.home() / '.codex')),
              'backend': str(Path(__file__).resolve().with_name('backend.py'))}
    signature = {**_copy_signature(Path(source_app), sync_bytes),
                 'display_name': name, 'bundle_id': profile_bundle_id(profile['id']),
                 'app_name': destination.name, 'dock_launch': marker}
    marker['engine_executable'] = signature['executable']
    if existing.exists():
        if not record_path.exists():
            raise DesktopCopyError('实例应用包缺少完成记录，未覆盖。')
        saved = json.loads(record_path.read_text())
        if all(saved.get(key) == value for key, value in signature.items()):
            verify_copy(existing, profile_id=profile['id'], display_name=name)
            _cleanup_recorded_previous(record_path)
            return existing
        if not allow_replace_stopped:
            raise DesktopCopyNeedsUpdate('该实例应用需要更新；请先关闭该实例。')
    template = ensure_desktop(root, source_app, allow_replace_stopped=allow_replace_stopped)
    staging = directory / ('.preparing-' + uuid.uuid4().hex + '.app')
    try:
        _run(['/bin/cp', '-cR', str(template), str(staging)])
        info_path = staging / 'Contents/Info.plist'
        info = plistlib.loads(info_path.read_bytes())
        info.update(CFBundleName=name, CFBundleDisplayName=name,
                    CFBundleIdentifier=signature['bundle_id'])
        info_path.write_bytes(plistlib.dumps(info))
        (staging / 'Contents/Resources/duo-instance.json').write_text(json.dumps(marker) + '\n')
        # Nested code is already signed in the template; only outer metadata differs.
        sign_app(staging, deep=False, identifier=BUNDLE_ID)
        verified = verify_copy(staging, profile_id=profile['id'], display_name=name)
        _install_copy(staging, destination, existing, record_path, {**signature, **verified})
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def cleanup_unused_template(root: Path, profiles: list[dict]) -> bool:
    root = _owned_root(root)
    if any((root / 'profiles' / p['id'] / 'desktop/copy.json').exists() for p in profiles):
        return False
    desktop = root / 'desktop'
    if (desktop / 'copy.json').exists():
        _cleanup_recorded_previous(desktop / 'copy.json')
    for filename in (APP_BUNDLE_NAME, LEGACY_APP_BUNDLE_NAME):
        app = desktop / filename
        if app.exists():
            if app.is_symlink() or plistlib.loads((app / 'Contents/Info.plist').read_bytes())['CFBundleIdentifier'] != BUNDLE_ID:
                raise DesktopCopyError('公共应用模板身份不匹配，未清理。')
            shutil.rmtree(app)
    (desktop / 'copy.json').unlink(missing_ok=True)
    if desktop.exists() and not any(desktop.iterdir()):
        desktop.rmdir()
    return True


def cleanup_profile_ipc(root: Path, profile: str) -> None:
    # Reuse the same deterministic location and ownership marker as creation.
    root = Path(root).resolve()
    directory = _ipc_path(root, profile)
    if not directory.exists():
        return
    marker = directory / '.orbit-owner.json'
    identity = {'owner': 'codex-dual', 'root': str(root), 'profile': profile}
    if (any(p.is_symlink() for p in (directory.parent.parent, directory.parent, directory))
            or marker.is_symlink() or json.loads(marker.read_text()) != identity):
        raise DesktopCopyError('实例 IPC 目录所有权不匹配，未清理。')
    shutil.rmtree(directory)
    for parent in (directory.parent, directory.parent.parent):
        if any(parent.iterdir()):
            break
        parent.rmdir()
