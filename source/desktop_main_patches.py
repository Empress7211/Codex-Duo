"""Locate managed desktop hooks by behavior, independently of build filenames.

Only the generated app copy is patched. A missing or ambiguous hook is an
unsupported contract, rather than a reason to produce a partly isolated app.
"""
from __future__ import annotations

import json
import re


class PatchError(RuntimeError):
    pass


_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
_EARLY_MARKER = "/*codex-duo:early-boundary*/"
_SYNC_MARKER = "/*codex-duo:sync-install*/"
_RELEASE_MARKER = "/*codex-duo:writer-release*/"
_IDENTITY_MARKER = "/*codex-duo:identity*/"
_IPC_MARKER = "/*codex-duo:ipc*/"
_TITLE_MARKER = "/*codex-duo:window-title*/"
_IPC_PATTERN = re.compile(
    r"\blet\s+(?P<directory>" + _IDENTIFIER + r")="
    r"(?P<expression>" + _IDENTIFIER + r"\.join\([^;]{1,200},"
    r"(?P<quote>[\"'`])ipc(?P=quote)\))"
    # Newer builds derive ipc.sock first and create/validate the directory
    # through a helper; older builds call mkdirSync directly.
    r"(?:;" + _IDENTIFIER + r"\.mkdirSync\((?P=directory),"
    r"\{mode:448,recursive:!0\}\)"
    r"|," + _IDENTIFIER + r"=" + _IDENTIFIER + r"\.join\((?P=directory),"
    r"[\"'`]ipc\.sock[\"'`]\)," + _IDENTIFIER + r"=" + _IDENTIFIER
    + r"\((?P=directory)\))"
)
_MARKERS = (_EARLY_MARKER, _SYNC_MARKER, _RELEASE_MARKER,
            _IDENTITY_MARKER, _IPC_MARKER, _TITLE_MARKER)
_SYNC_INSTALL = (
    _SYNC_MARKER
    + "if(!this.__codexDuoSyncInstalled){"
    "this.disposables.add(require(require('node:path').join("
    "process.resourcesPath,'orbit-sync.cjs')).install(this.scopedState,"
    "this.options.windowManager,{invalidateQuery:e=>"
    "this.broadcastQueryCacheInvalidation(e),invalidateCatalog:()=>"
    "this.localThreadCatalogSyncCoordinator?.invalidateSource()}));"
    "this.__codexDuoSyncInstalled=!0;}"
)
_EARLY_SUFFIX = (
    "\n}else{console.error('Launch this managed desktop from Codex Duo.');"
    "orbitEarlyApp.exit(0);}\n"
)


def _early_prefix(app_name: str) -> str:
    return (
        _EARLY_MARKER
        + "const orbitEarlyApp=require('electron').app;"
        "const orbitEarlyPath=require('node:path');"
        "if(process.env.ORBIT_ROOT&&process.env.CODEX_HOME&&"
        "process.env.ORBIT_SOURCE_HOME&&process.env.CODEX_ELECTRON_USER_DATA_PATH&&"
        "orbitEarlyPath.resolve(process.env.CODEX_HOME)!=="
        "orbitEarlyPath.resolve(process.env.ORBIT_SOURCE_HOME)){"
        "process.env.CODEX_SPARKLE_ENABLED='false';"
        "orbitEarlyApp.setName(" + json.dumps(app_name) + ");"
    )


def _title_patch(display_name: str) -> str:
    return (_TITLE_MARKER + "(process.env.ORBIT_PROFILE_NAME||"
            + json.dumps(display_name) + ")")


def _texts(scripts: dict[str, bytes]) -> dict[str, str]:
    return {name: body.decode("utf-8") for name, body in scripts.items()
            if name.endswith(".js")}


def _one(matches: list, description: str):
    if len(matches) != 1:
        raise PatchError(f"Codex {description}需唯一匹配，实际找到 {len(matches)} 处；未生成副本。")
    return matches[0]


def _patch_ipc(texts: dict[str, str], patched: dict[str, str]) -> str:
    name, _ = _one([
        (name, match) for name, text in texts.items()
        if "ipc.sock" in text
        for match in _IPC_PATTERN.finditer(text)
    ], "独立 IPC 目录接入位置")
    ipc = patched[name]
    match = _one(list(_IPC_PATTERN.finditer(ipc)), "独立 IPC 目录接入位置")
    start, end = match.span("expression")
    patched[name] = (ipc[:start] + _IPC_MARKER + "process.env.ORBIT_IPC_DIR||"
                     + ipc[start:end] + ipc[end:])
    return name


def _package(scripts: dict[str, bytes]) -> tuple[dict, str]:
    try:
        package = json.loads(scripts["package.json"])
        entry = package["main"].removeprefix("./")
        if entry not in scripts or not entry.endswith(".js"):
            raise ValueError("main is not an available JavaScript entry")
        return package, entry
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise PatchError("Codex package.json 没有可用的 JavaScript 启动入口。") from error


def _closing_parenthesis(text: str, opening: int) -> int:
    """Read the small setName argument without depending on its minified names."""
    depth = 0
    quote = None
    escaped = False
    for position in range(opening, len(text)):
        character = text[position]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
        elif character in "\"'`":
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return position
        elif character in ";{}":
            break
    raise PatchError("Codex 应用名称表达式不符合受管替换契约。")


def discover_main_patches(scripts: dict[str, bytes], app_name: str,
                          display_name: str) -> dict[str, bytes]:
    """Return all required main-process changes, or fail before writing anything.

    ``scripts`` contains package.json and the app's main-process JavaScript
    chunks. Chunk names and compressed local identifiers are discovered from
    the actual source. The existing synchronization APIs must remain present.
    """
    package, early_name = _package(scripts)
    texts = _texts(scripts)
    if any(marker in text for text in texts.values() for marker in _MARKERS):
        raise PatchError("桌面输入已含 Codex Duo 补丁，请选择本机官方 Codex 应用。")
    patched = dict(texts)
    changed = {early_name}

    main_name = _one([
        name for name, text in texts.items()
        if "createAppHost(" in text and "this.scopedState" in text
        and "broadcastQueryCacheInvalidation" in text
    ], "桌面同步模块")
    main = patched[main_name]
    required_apis = ("this.disposables.add(", "this.options.windowManager",
                     "localThreadCatalogSyncCoordinator",
                     "markAllConversationsNeedResumeAfterReconnect")
    if any(api not in main for api in required_apis):
        raise PatchError("Codex 桌面同步或会话恢复 API 已变化；未生成副本。")
    method = _one(list(re.finditer(r"(?<![\w$.])createAppHost\([^{};]*\)\{", main)),
                  "桌面同步接入方法")
    # Installing at first host creation avoids recognizing constructor-local
    # variables and still runs after scoped state and the catalog exist.
    main = main[:method.end()] + _SYNC_INSTALL + main[method.end():]
    release_pattern = re.compile(
        r"(?<=[;}])(?P<manager>" + _IDENTIFIER + r")\.onNotification\("
        r"(?P<notification>" + _IDENTIFIER + r")\.method,"
        r"(?P=notification)\.params,"
    )
    release = _one(list(release_pattern.finditer(main)), "会话释放通知接入位置")
    manager, notification = release.group("manager", "notification")
    release_patch = (
        _RELEASE_MARKER
        + f"if({notification}.method==='thread/status/changed'&&"
        f"{notification}.params.orbitReleased===!0){{"
        f"{manager}.markAllConversationsNeedResumeAfterReconnect({{"
        "restoreStreams:!1,foregroundConversationId:null}),"
        "require(require('node:path').join(process.resourcesPath,'orbit-sync.cjs'))"
        f".notifyWriterRelease({notification}.params.threadId)}};"
    )
    main = main[:release.start()] + release_patch + main[release.start():]
    title_pattern = re.compile(
        r"(?P<prefix>title:(?:" + _IDENTIFIER + r"\?\?)?)"
        + _IDENTIFIER + r"\.app\.getName\(\)"
    )
    main, title_count = title_pattern.subn(
        lambda match: match.group("prefix") + _title_patch(display_name), main)
    if not title_count:
        raise PatchError("Codex 窗口名称接入位置已变化；未生成副本。")
    patched[main_name] = main
    changed.add(main_name)

    identity_pattern = re.compile(r"(?<![\w$.])" + _IDENTIFIER + r"\.app\.setName\(")
    identity_name, identity_match = _one([
        (name, match) for name, text in texts.items()
        for match in identity_pattern.finditer(text)
    ], "应用名称接入位置")
    identity = patched[identity_name]
    if ("CODEX_ELECTRON_USER_DATA_PATH" not in identity
            or not re.search(r"\.app\.setPath\([\"'`]userData[\"'`]", identity)):
        raise PatchError("Codex 没有独立 userData 启动入口；未生成副本。")
    # This expression's location may have shifted if it shares the main chunk.
    identity_match = _one(list(identity_pattern.finditer(identity)), "应用名称接入位置")
    closing = _closing_parenthesis(identity, identity_match.end() - 1)
    identity = (identity[:identity_match.start()] + _IDENTITY_MARKER
                + identity[identity_match.start():identity_match.end()]
                + json.dumps(app_name) + identity[closing:])
    patched[identity_name] = identity
    changed.add(identity_name)

    changed.add(_patch_ipc(texts, patched))

    patched[early_name] = _early_prefix(app_name) + patched[early_name] + _EARLY_SUFFIX
    package.update(name="codex-orbit-desktop", productName=app_name)
    result = {name: patched[name].encode("utf-8") for name in changed}
    result["package.json"] = (json.dumps(package, ensure_ascii=False, indent=2) + "\n").encode()
    validate_main_patches({**scripts, **result}, app_name, display_name)
    return result


def validate_main_patches(scripts: dict[str, bytes], app_name: str,
                          display_name: str) -> None:
    """Verify the managed contracts using the discovered scripts and markers."""
    package, entry = _package(scripts)
    texts = _texts(scripts)
    if package.get("name") != "codex-orbit-desktop" or package.get("productName") != app_name:
        raise PatchError("受管桌面 package 身份不匹配。")
    if not (texts[entry].startswith(_early_prefix(app_name))
            and texts[entry].endswith(_EARLY_SUFFIX)):
        raise PatchError("受管桌面早期隔离启动边界不匹配。")
    for marker in _MARKERS[:-1]:
        count = sum(text.count(marker) for text in texts.values())
        if count != 1:
            raise PatchError(f"受管桌面补丁标记 {marker} 不唯一（{count} 处）。")
    main = _one([text for text in texts.values() if _SYNC_MARKER in text], "受管桌面同步模块")
    if (_SYNC_INSTALL not in main or _RELEASE_MARKER not in main
            or ".notifyWriterRelease(" not in main
            or ".markAllConversationsNeedResumeAfterReconnect({" not in main
            or "params.orbitReleased===!0" not in main
            or _title_patch(display_name) not in main
            or main.count(_TITLE_MARKER) != main.count(_title_patch(display_name))):
        raise PatchError("受管桌面同步、写入权释放或窗口名称补丁不完整。")
    identity = _one([text for text in texts.values() if _IDENTITY_MARKER in text], "受管应用名称模块")
    if not re.search(re.escape(_IDENTITY_MARKER) + _IDENTIFIER
                     + r"\.app\.setName\(" + re.escape(json.dumps(app_name)) + r"\)", identity):
        raise PatchError("受管桌面独立应用名称不匹配。")
    ipc = _one([text for text in texts.values() if _IPC_MARKER in text], "受管 IPC 模块")
    if _IPC_MARKER + "process.env.ORBIT_IPC_DIR||" not in ipc:
        raise PatchError("受管桌面独立 IPC 目录补丁不完整。")
