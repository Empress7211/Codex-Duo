"""Build the pinned desktop copy used only by Orbit-managed profiles.

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


BUILD = "20398"
MAIN_FILE = ".vite/build/main-CwxgTOle.js"
MAIN_SHA256 = "87d52265eb13608a380fabcac47ecf13a39b981115c0960c18af563ea6d32d51"
SEAM = "})}createAppHost(e){let t=G5(this.options,e),n="
BOOTSTRAP = (
    "}),this.disposables.add(require(require('node:path').join("
    "process.resourcesPath,'orbit-sync.cjs')).install(this.scopedState,"
    "this.options.windowManager,{invalidateQuery:e=>"
    "this.broadcastQueryCacheInvalidation(e),invalidateCatalog:()=>"
    "this.localThreadCatalogSyncCoordinator?.invalidateSource()}))}createAppHost(e){"
    "let t=G5(this.options,e),n="
)
DISABLE_UPDATER = "process.env.CODEX_SPARKLE_ENABLED='false';"
BUNDLE_ID = "local.codex-orbit.desktop"
# Electron's name is also the existing encrypted-storage identity.
APP_NAME = "Codex Duo Desktop"
DISPLAY_NAME = "Codex Duo Instance"
APP_BUNDLE_NAME = DISPLAY_NAME + ".app"
LEGACY_APP_BUNDLE_NAME = "Codex Orbit Instance.app"
WINDOW_TITLE_SEAM = "title:_.app.getName()"
WINDOW_TITLE_PATCH = "title:(process.env.ORBIT_PROFILE_NAME||" + json.dumps(DISPLAY_NAME) + ")"
EARLY_FILE = ".vite/build/early-bootstrap.js"
EARLY_SHA256 = "036bde993c50a1a34d0f7a0ee4f3d57b5592b4cfe3b2a87b4022b263bf0367aa"
IDENTITY_FILE = ".vite/build/bootstrap-DOZRFVdo.js"
IDENTITY_SHA256 = "8602f1022fc835148268e8cdfcfb939d8ab5a969e4fd0b4141e264ce8ec4be4d"
PACKAGE_FILE = "package.json"
PACKAGE_SHA256 = "9157288fa1fd802710f4021bb6155681274c40a41e5576098277e9c223d888f7"
APP_NAME_SEAM = "f.app.setName(r.Ut(Hk))"
APP_NAME_PATCH = "f.app.setName(" + json.dumps(APP_NAME) + ")"
INTEGRITY_SENTINEL = b"AGbevlPCksUGKNL8TSn7wGmJEuJsXb2A"
FRAMEWORK_FILE = "Contents/Frameworks/Codex Framework.framework/Versions/155.0.8059.40/Codex Framework"
SHARED_FILE = "webview/assets/app-shared-8340f229c2b3.js"
INITIAL_FILE = "webview/assets/app-initial-1ab6fbe93c4f.js"
RESUME_FILE = "webview/assets/use-resume-conversation-if-needed-3bb489937654.js"
LOCAL_CONVERSATION_FILE = "webview/assets/local-conversation-thread-269c4ee9ddf0.js"
SCRIPT_HASHES = {
    SHARED_FILE: "3b4cabc382e71f0da7398393a619d7c62d13dfae598a5fe2e9880ab8d31d1bf5",
    INITIAL_FILE: "f3d5fedc993d0ead2720c9877d8c72a82dc5fb2d935f13ad9426e6964fb6f105",
    RESUME_FILE: "bb6bb8af0065acef78159e359240613223618a2b6941c1f27651a862e3dc47ff",
    LOCAL_CONVERSATION_FILE: "9b5561cdb97fb19052f3f7ed0b15cd061a4285f694a19d31fb092db7b35afbb8",
}
PATCH_VERSION = 18
KEYCHAIN_SERVICE_SUFFIX = b" Storage Key\0"
# Preserve Mach-O string offsets. The native service prefix is "Codex".
DUO_KEYCHAIN_SERVICE_SUFFIX = b" Duo Vault\0\0\0"
IPC_FILE = ".vite/build/application-network-startup-YSC7OAs0.js"
IPC_SHA256 = "52f0be375fb08281e88ad41be64220fb73f85210523e53e7d7acf094fda6344e"
MAIN_RELEASE_SEAM = "e.onNotification(i.method,i.params,n)"
MAIN_RELEASE_PATCH = (
    "if(i.method===`thread/status/changed`&&i.params.orbitReleased===!0){"
    "e.markAllConversationsNeedResumeAfterReconnect({restoreStreams:!1,"
    "foregroundConversationId:null}),require(require('node:path').join("
    "process.resourcesPath,'orbit-sync.cjs')).notifyWriterRelease(i.params.threadId)};"
    + MAIN_RELEASE_SEAM
)


class DesktopCopyError(RuntimeError):
    pass


class DesktopCopyNeedsUpdate(DesktopCopyError):
    """The managed desktop is valid, but its pinned patch is out of date."""


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


def patch_main(body: bytes) -> bytes:
    if hashlib.sha256(body).hexdigest() != MAIN_SHA256:
        raise DesktopCopyError("Codex 桌面脚本已变化，尚未核验此版本。")
    text = body.decode("utf-8")
    if text.count(SEAM) != 1:
        raise DesktopCopyError("Codex 桌面同步接入位置与已核验版本不同。")
    if text.count(MAIN_RELEASE_SEAM) != 1:
        raise DesktopCopyError("Codex 会话释放接入位置与已核验版本不同。")
    if text.count(WINDOW_TITLE_SEAM) != 4:
        raise DesktopCopyError("Codex 窗口命名位置与已核验版本不同。")
    text = text.replace(SEAM, BOOTSTRAP, 1).replace(MAIN_RELEASE_SEAM, MAIN_RELEASE_PATCH, 1)
    text = text.replace(WINDOW_TITLE_SEAM, WINDOW_TITLE_PATCH)
    return (DISABLE_UPDATER + text).encode("utf-8")


def patch_early_bootstrap(body: bytes, app_name: str = APP_NAME) -> bytes:
    if hashlib.sha256(body).hexdigest() != EARLY_SHA256:
        raise DesktopCopyError("Codex 早期启动脚本已变化，尚未核验此版本。")
    # Directly opening this generated app omits the isolated launcher env. Do
    # not evaluate native startup modules that would then use the official home.
    prefix = (
        "const orbitEarlyApp=require('electron').app;"
        "const orbitEarlyPath=require('node:path');"
        "if(process.env.ORBIT_ROOT&&process.env.CODEX_HOME&&"
        "process.env.ORBIT_SOURCE_HOME&&process.env.CODEX_ELECTRON_USER_DATA_PATH&&"
        "orbitEarlyPath.resolve(process.env.CODEX_HOME)!=="
        "orbitEarlyPath.resolve(process.env.ORBIT_SOURCE_HOME)){"
        + DISABLE_UPDATER + "orbitEarlyApp.setName(" + json.dumps(app_name) + ");"
    )
    suffix = (
        "\n}else{console.error('Launch this managed desktop from Codex Duo.');"
        "orbitEarlyApp.exit(0);}\n"
    )
    return prefix.encode() + body + suffix.encode()


def patch_identity_bootstrap(body: bytes, app_name: str = APP_NAME) -> bytes:
    if hashlib.sha256(body).hexdigest() != IDENTITY_SHA256:
        raise DesktopCopyError("Codex 应用命名脚本已变化，尚未核验此版本。")
    if body.count(APP_NAME_SEAM.encode()) != 1:
        raise DesktopCopyError("Codex 应用命名位置与已核验版本不同。")
    # Keep the JS storage name independent of per-instance display names.
    # The native framework's service namespace is patched separately below.
    patch = "f.app.setName(" + json.dumps(app_name) + ")"
    return body.replace(APP_NAME_SEAM.encode(), patch.encode(), 1)


def patch_package_identity(body: bytes, app_name: str = APP_NAME) -> bytes:
    if hashlib.sha256(body).hexdigest() != PACKAGE_SHA256:
        raise DesktopCopyError("Codex package 元数据已变化，尚未核验此版本。")
    package = json.loads(body)
    package["name"] = "codex-orbit-desktop"
    package["productName"] = app_name
    return (json.dumps(package, indent=2, ensure_ascii=False) + "\n").encode()


def patch_runtime_script(name: str, body: bytes) -> bytes:
    if hashlib.sha256(body).hexdigest() != SCRIPT_HASHES[name]:
        raise DesktopCopyError("Codex 运行状态脚本已变化，尚未核验此版本。")
    text = body.decode("utf-8")
    if name == SHARED_FILE:
        seam = "let a=Y(r);if(!t.threadStore.conversations.has(a)){let e=t.threadStore.recordThreadRuntimeStatusEvidence(r,i);"
        patch = (
            "let a=Y(r);if(n.params.orbitReleased===!0){"
            "e.inactiveThreadUnsubscriber.clearConversationStreamOwnership(a),"
            "e.updateConversationState(a,e=>{e.resumeState=`needs_resume`});"
            "window.dispatchEvent(new CustomEvent('orbit-writer-locks-changed',"
            "{detail:{threadId:r,has_writer:!1}}))};"
            "if(!t.threadStore.conversations.has(a)){let e=t.threadStore.recordThreadRuntimeStatusEvidence(r,i);"
        )
    elif name == INITIAL_FILE:
        seam = "case`workspace-root-options-updated`:f.invalidateQueries"
        patch = (
            "case`orbit-writer-locks-changed`:window.dispatchEvent(new CustomEvent("
            "'orbit-writer-locks-changed',{detail:e}));break bb460;"
            "case`orbit-writer-handoff-available`:window.dispatchEvent(new CustomEvent("
            "'orbit-writer-handoff-available',{detail:e}));break bb460;"
            "case`workspace-root-options-updated`:f.invalidateQueries"
        )
    elif name == RESUME_FILE:
        seam = "let I=P,L;"
        patch = (
            "let I=P;(0,B.useLayoutEffect)(()=>{x(g)},[e,g]);"
            "(0,B.useEffect)(()=>{let t=t=>{"
            "t.detail?.threadId===e&&(t.detail?.has_writer===!1||"
            "t.type==='orbit-writer-handoff-available'&&t.detail?.handoff_available===!0)&&"
            "A.current===e&&fe===e&&I()};"
            "return window.addEventListener('orbit-writer-locks-changed',t),"
            "window.addEventListener('orbit-writer-handoff-available',t),"
            "()=>{window.removeEventListener('orbit-writer-locks-changed',t);"
            "window.removeEventListener('orbit-writer-handoff-available',t)}},[e,fe,I]);let L;"
        )
        message = (
            "id:`localTaskRow.resumeLiveWriterError`,defaultMessage:`This task is active elsewhere."
            "{br}Close it there, then retry here.`"
        )
        replacement = (
            "id:`orbit.resumeWriterHeld`,defaultMessage:e.locale.toLowerCase().startsWith(`zh`)?"
            "`另一窗口仍占有此会话的写入权。{br}任务可能已经空闲；等待写入权释放后重试。`:"
            "`Another window still holds this chat's writer.{br}The task may be idle; retry after the writer is released.`"
        )
        if text.count(message) != 1:
            raise DesktopCopyError("Codex 写入权错误提示与已核验版本不同。")
        text = text.replace(message, replacement, 1)
    else:
        replacements = {
            "function wj(e){let t=(0,Fj.c)(21)":
                "function wj(e){let t=(0,Fj.c)(23)",
            "let _=g,v,y,b;t[9]===Symbol.for(`react.memo_cache_sentinel`)?":
                "let _=g,v,y,b,orbitChinese=Ai().locale.toLowerCase().startsWith(`zh`);"
                "t[9]===Symbol.for(`react.memo_cache_sentinel`)||t[21]!==orbitChinese?",
            "t[9]=v,t[10]=y,t[11]=b):(v=t[9],y=t[10],b=t[11]);let x;":
                "t[9]=v,t[10]=y,t[11]=b,t[21]=orbitChinese):"
                "(v=t[9],y=t[10],b=t[11]);let x;",
            "t[16]!==p||t[17]!==i||t[18]!==x||t[19]!==S?":
                "t[16]!==p||t[17]!==i||t[18]!==x||t[19]!==S||t[22]!==orbitChinese?",
            "t[18]=x,t[19]=S,t[20]=C):C=t[20],C}function Tj":
                "t[18]=x,t[19]=S,t[20]=C,t[22]=orbitChinese):C=t[20],C}function Tj",
            "let B=!_||F,V=M&&!F,ee=_&&!F,H;":
                "let B=!_||F||M,V=M&&!F,ee=_&&!F&&!M,H;",
            "let Et=Oe.visibleSurface!=null,Dt=W==null||Ke&&!Ye,Ot=M||Re||Ye,J=Ke&&!Ye,kt;":
                "let Et=Oe.visibleSurface!=null,Dt=W==null||Ke&&!Ye,Ot=M||Re||Ye||Ke,J=Ke&&!Ye,kt;",
            "let At=F&&!Re&&!Ye,jt;": "let At=F&&!Re&&!Ye&&!Ke,jt;",
            "submitDisabled:i||j||F,isThreadHandoffInProgress:F":
                "submitDisabled:i||j||F||c,isThreadHandoffInProgress:F",
            "id:`localConversation.writerConflict.title`,defaultMessage:`This is open in another app`":
                "id:`orbit.writerHeld.title`,defaultMessage:orbitChinese?"
                "`另一窗口占有此会话的写入权`:`Another window holds this chat's writer`",
            "id:`localConversation.writerConflict.description`,defaultMessage:`Close it there to continue here.`":
                "id:`orbit.writerHeld.description`,defaultMessage:orbitChinese?"
                "`任务可能已经空闲，但后端仍占有写入权。等待释放后重试。`:"
                "`The task may be idle, but its backend still holds the writer. Retry after it releases the chat.`",
        }
        for original, replacement in replacements.items():
            if text.count(original) != 1:
                raise DesktopCopyError("Codex 会话输入或占用提示与已核验版本不同。")
            text = text.replace(original, replacement, 1)
        return text.encode("utf-8")
    if text.count(seam) != 1:
        raise DesktopCopyError("Codex 运行状态接入位置与已核验版本不同。")
    return text.replace(seam, patch, 1).encode("utf-8")


def patch_ipc_script(body: bytes) -> bytes:
    if hashlib.sha256(body).hexdigest() != IPC_SHA256:
        raise DesktopCopyError("Codex IPC 脚本已变化，尚未核验此版本。")
    seam = "let e=s.join(xr(),`ipc`);a.mkdirSync(e,{mode:448,recursive:!0});"
    if body.count(seam.encode()) != 1:
        raise DesktopCopyError("Codex IPC 目录接入位置与已核验版本不同。")
    return body.replace(seam.encode(),
                        b"let e=process.env.ORBIT_IPC_DIR||s.join(xr(),`ipc`);a.mkdirSync(e,{mode:448,recursive:!0});", 1)


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
        raise DesktopCopyError(result.stderr.strip() or "桌面副本命令执行失败。")


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
    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    asar = app / "Contents/Resources/app.asar"
    header, _, header_json = read_asar_header(asar)
    header_hash = hashlib.sha256(header_json).hexdigest()
    integrity = plist.get("ElectronAsarIntegrity", {}).get("Resources/app.asar", {})
    if integrity != {"algorithm": "SHA256", "hash": header_hash}:
        raise DesktopCopyError("受管 app.asar 与 Electron 头校验不一致。")
    body = read_asar_file(asar, MAIN_FILE)
    digest = hashlib.sha256(body).hexdigest()
    if (digest != _entry(header, MAIN_FILE)["integrity"]["hash"] or
            BOOTSTRAP.encode() not in body or not body.startswith(DISABLE_UPDATER.encode())):
        raise DesktopCopyError("受管桌面同步脚本校验失败。")
    if not allow_legacy_display and (body.count(WINDOW_TITLE_PATCH.encode()) != 4
                                     or WINDOW_TITLE_SEAM.encode() in body):
        raise DesktopCopyError("受管窗口标题尚未更新为 Codex Duo。")
    expected_id = profile_bundle_id(profile_id) if profile_id else BUNDLE_ID
    if plist.get("CFBundleIdentifier") != expected_id:
        raise DesktopCopyError("受管桌面副本没有独立应用标识。")
    names = {DISPLAY_NAME, APP_NAME} if allow_legacy_display else {display_name or DISPLAY_NAME}
    if (plist.get("CFBundleName") not in names or
            plist.get("CFBundleDisplayName") != plist.get("CFBundleName")):
        raise DesktopCopyError("受管桌面副本没有独立应用名称。")
    identity = read_asar_file(asar, IDENTITY_FILE)
    early = read_asar_file(asar, EARLY_FILE)
    package = read_asar_file(asar, PACKAGE_FILE)
    if (APP_NAME_PATCH.encode() not in identity or
            ("orbitEarlyApp.setName(" + json.dumps(APP_NAME) + ")").encode() not in early or
            b"orbitEarlyApp.exit(0)" not in early or
            json.loads(package).get("productName") != APP_NAME):
        raise DesktopCopyError("受管桌面启动边界或钥匙串命名校验失败。")
    for name, script in [(IDENTITY_FILE, identity), (EARLY_FILE, early), (PACKAGE_FILE, package)]:
        if hashlib.sha256(script).hexdigest() != _entry(header, name)["integrity"]["hash"]:
            raise DesktopCopyError("受管应用身份脚本摘要校验失败。")
    _, framework_digest = framework_integrity_digest(app / FRAMEWORK_FILE)
    if framework_digest != integrity_dictionary_digest(plist["ElectronAsarIntegrity"]):
        raise DesktopCopyError("Electron 内嵌字典摘要与受管 app.asar 不一致。")
    with (app / FRAMEWORK_FILE).open("rb") as stream, mmap.mmap(
            stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
        if (mapped.find(DUO_KEYCHAIN_SERVICE_SUFFIX) < 0 or
                mapped.find(KEYCHAIN_SERVICE_SUFFIX) >= 0):
            raise DesktopCopyError("受管桌面仍会访问官方存储钥匙串。")
    for name in SCRIPT_HASHES:
        script = read_asar_file(asar, name)
        marker = b"orbit.writerHeld.title" if name == LOCAL_CONVERSATION_FILE else b"orbit-writer-locks-changed"
        if (hashlib.sha256(script).hexdigest() != _entry(header, name)["integrity"]["hash"] or
                marker not in script):
            raise DesktopCopyError("受管运行状态脚本校验失败。")
    ipc_script = read_asar_file(asar, IPC_FILE)
    if (b"process.env.ORBIT_IPC_DIR" not in ipc_script or
            hashlib.sha256(ipc_script).hexdigest() != _entry(header, IPC_FILE)["integrity"]["hash"]):
        raise DesktopCopyError("受管 IPC 脚本校验失败。")
    if profile_id:
        marker = json.loads((app / 'Contents/Resources/duo-instance.json').read_text())
        if marker['profile_id'] != profile_id:
            raise DesktopCopyError("实例应用包的启动身份不匹配。")
        # Different Dock identities are variants of the same managed code and
        # must keep the prior storage-access signing identity, not change the key.
        _run(['/usr/bin/codesign', '--verify', '-R', '=identifier "' + BUNDLE_ID + '"', str(app)])
    if not allow_legacy_display and plist.get('CFBundleExecutable') != 'DuoInstance':
        raise DesktopCopyError('受管应用没有原生隔离启动入口。')
    if not allow_legacy_display:
        _run(['/usr/bin/codesign', '--verify', '-R', '=identifier "' + BUNDLE_ID + '"',
              str(app / 'Contents/MacOS/ChatGPT')])
    _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    return {"build": plist["CFBundleVersion"], "header_sha256": header_hash,
            "main_sha256": digest, "signature_verified": True}


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
    """Create/reuse a verified pinned app in root/desktop; never launch it."""
    root = _owned_root(root)
    source_app = Path(source_app).expanduser().resolve()
    destination = root / "desktop" / APP_BUNDLE_NAME
    existing = managed_app_path(root)
    if source_app.is_relative_to(root):
        raise DesktopCopyError("官方桌面输入不得来自 Orbit 生成目录。")
    source_plist = plistlib.loads((source_app / "Contents/Info.plist").read_bytes())
    if source_plist.get("CFBundleVersion") != BUILD or source_plist.get("CFBundleIdentifier") != "com.openai.codex":
        raise DesktopCopyError("尚未核验此 Codex 版本，未生成桌面副本。")
    sync_module = Path(sync_module or Path(__file__).with_name("managed_sync.js"))
    sync_bytes = sync_module.read_bytes()
    packaged = Path(__file__).resolve().parent.parent / "desktop" / APP_BUNDLE_NAME
    signature = {"build": BUILD, "patch_version": PATCH_VERSION,
                 "launcher_sha256": hashlib.sha256(Path(__file__).with_name('duo-instance-launcher').read_bytes()).hexdigest(),
                 "sync_sha256": hashlib.sha256(sync_bytes).hexdigest()}
    if packaged.is_dir():
        # The publisher signs this engine once. Local instances inherit it,
        # and users never need a publisher certificate/private key.
        record = json.loads(packaged.with_name("copy.json").read_text())
        if any(record.get(key) != value for key, value in signature.items()):
            raise DesktopCopyError("随应用提供的运行引擎与启动器版本不匹配。")
        # A publisher DR intentionally survives releases. The signed template's
        # CDHash also covers its resource seal and distinguishes changed code.
        signature["packaged_engine"] = record["engine_cdhash"]
    record_path = root / "desktop/copy.json"
    if existing.exists() and record_path.exists():
        saved = json.loads(record_path.read_text())
        if existing == destination and all(saved.get(key) == value for key, value in signature.items()):
            verify_copy(destination)
            _cleanup_recorded_previous(record_path)
            return destination
        if not allow_replace_stopped:
            raise DesktopCopyNeedsUpdate("受管桌面副本需要更新；请先关闭受管实例。")
    elif existing.exists():
        raise DesktopCopyError("受管副本已有文件但无完成记录，未覆盖。")
    original_main = read_asar_file(source_app / "Contents/Resources/app.asar", MAIN_FILE)
    patched_main = patch_main(original_main)
    patched_runtime = {name: patch_runtime_script(name, read_asar_file(
        source_app / "Contents/Resources/app.asar", name)) for name in SCRIPT_HASHES}
    patched_ipc = patch_ipc_script(read_asar_file(source_app / "Contents/Resources/app.asar", IPC_FILE))
    patched_identity = {
        EARLY_FILE: patch_early_bootstrap(read_asar_file(source_app / "Contents/Resources/app.asar", EARLY_FILE)),
        IDENTITY_FILE: patch_identity_bootstrap(read_asar_file(source_app / "Contents/Resources/app.asar", IDENTITY_FILE)),
        PACKAGE_FILE: patch_package_identity(read_asar_file(source_app / "Contents/Resources/app.asar", PACKAGE_FILE)),
    }
    staging = root / "desktop" / (".preparing-" + uuid.uuid4().hex + ".app")
    try:
        if packaged.is_dir():
            verify_copy(packaged)
            _run(["/bin/cp", "-cR", str(packaged), str(staging)])
            verified = verify_copy(staging)
            _install_copy(staging, destination, existing, record_path, {**signature, **verified})
            return destination
        # Darwin's -c uses APFS copy-on-write clones for regular files. Bundle
        # symlinks stay symlinks. Patch/sign only after cloning has completed.
        _run(["/bin/cp", "-cR", str(source_app), str(staging)])
        resources = staging / "Contents/Resources"
        (resources / "orbit-sync.cjs").write_bytes(sync_bytes)
        launcher = staging / 'Contents/MacOS/DuoInstance'
        shutil.copyfile(Path(__file__).with_name('duo-instance-launcher'), launcher)
        launcher.chmod(0o755)
        header_hash = replace_asar_files(resources / "app.asar",
                                        {MAIN_FILE: patched_main, **patched_runtime, IPC_FILE: patched_ipc,
                                         **patched_identity})
        copy_plist = dict(source_plist)
        copy_plist["CFBundleName"] = DISPLAY_NAME
        copy_plist["CFBundleDisplayName"] = DISPLAY_NAME
        copy_plist["CFBundleIdentifier"] = BUNDLE_ID
        copy_plist['CFBundleExecutable'] = 'DuoInstance'
        copy_plist["NSDocumentsFolderUsageDescription"] = (
            "仅在你打开“文稿”中的项目时，此额外 Codex 窗口才需要访问该项目文件。"
        )
        # Let the installed official app retain ownership of its URL scheme.
        # app-server subscription login uses its own localhost OAuth callback.
        copy_plist.pop("CFBundleURLTypes", None)
        copy_plist["ElectronAsarIntegrity"] = {
            **copy_plist.get("ElectronAsarIntegrity", {}),
            "Resources/app.asar": {"algorithm": "SHA256", "hash": header_hash},
        }
        update_framework_integrity(staging / FRAMEWORK_FILE,
                                   source_plist["ElectronAsarIntegrity"],
                                   copy_plist["ElectronAsarIntegrity"])
        patch_keychain_service(staging / FRAMEWORK_FILE)
        with (staging / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump(copy_plist, stream)
        sign_app(staging, identity=signing_identity)
        # --deep derives an identifier for the now-nested engine. Restore the
        # prior storage-access identity, then seal its signature in the wrapper.
        sign_app(staging / 'Contents/MacOS/ChatGPT', deep=False, identifier=BUNDLE_ID,
                 identity=signing_identity)
        sign_app(staging, deep=False, identifier=BUNDLE_ID, identity=signing_identity)
        verified = verify_copy(staging)
        _install_copy(staging, destination, existing, record_path, {**signature, **verified})
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def ensure_desktop(root: Path, source_app: Path = Path("/Applications/ChatGPT.app"),
                   *, allow_replace_stopped: bool = False) -> Path:
    """Allow replacement only after the caller has stopped all managed profiles."""
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
    signature = {'build': BUILD, 'patch_version': PATCH_VERSION,
                 'launcher_sha256': hashlib.sha256(Path(__file__).with_name('duo-instance-launcher').read_bytes()).hexdigest(),
                 'sync_sha256': hashlib.sha256(sync_bytes).hexdigest(),
                 'display_name': name, 'bundle_id': profile_bundle_id(profile['id']),
                 'app_name': destination.name, 'dock_launch': marker}
    packaged = Path(__file__).resolve().parent.parent / 'desktop' / APP_BUNDLE_NAME
    if packaged.is_dir():
        signature['packaged_engine'] = json.loads(packaged.with_name('copy.json').read_text())['engine_cdhash']
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
