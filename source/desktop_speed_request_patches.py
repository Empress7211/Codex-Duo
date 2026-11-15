"""Patch the native service-tier request path without changing account entitlement.

The native desktop suppresses service tiers for API-key accounts.  We only
supply local Fast access for that authentication method, using the host's
resolved config.  Subscription entitlement remains owned by Codex.
"""
from __future__ import annotations

import re


class PatchError(RuntimeError):
    pass


ID = r"[A-Za-z_$][\w$]*"
HOOK_MARKER = "orbit-api-speed-request-hook"
REQUEST_MARKER = "orbit-api-speed-request-resolver"


def _one(pattern: str, text: str, description: str) -> re.Match:
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise PatchError(f"Codex {description}需唯一匹配，实际 {len(matches)} 处；未更新实例。")
    return matches[0]


def _module_start(text: str, anchor: str | tuple[str, ...], required: tuple[str, ...], description: str) -> tuple[int, int]:
    # Webpack module identifiers vary by build; locate the nearest factory
    # preceding the semantic contract and verify the rest of that contract.
    factories = re.compile(rf"(?<![\w$]){ID}\({ID},{ID},{ID}\)\{{(?:var |{ID}\.d\()")
    candidates = []
    anchors = (anchor,) if isinstance(anchor, str) else anchor
    for marker in re.finditer('|'.join(re.escape(value) for value in anchors), text):
        earlier = list(factories.finditer(text, 0, marker.start()))
        if not earlier:
            continue
        start = earlier[-1].start()
        module = text[start:marker.end()]
        if all(value in module for value in required):
            candidates.append((start, marker.end()))
    if len(candidates) != 1:
        raise PatchError(f"Codex {description}需唯一匹配，实际 {len(candidates)} 处；未更新实例。")
    return candidates[0]


def _hook(text: str) -> str:
    # The module is duplicated in webpack async chunks.  Patching every copy
    # avoids depending on which chunk registers the module first.
    start, end = _module_start(text, "clearPrewarmedThreads():void 0",
                               ("serviceTierAccess:", "serviceTierForRequest:",
                                "setDefaultServiceTierConfig", "updateThreadSettingsForNextTurn"),
                               "速度设置模块")
    module = text[start:end]
    account = _one(rf"(?P<account>{ID})=\(0,{ID}\.tn\)\((?P<host>{ID})\)", module, "速度设置身份")
    query = _one(
        rf"(?P<query>{ID})=\(0,(?P<hook>{ID})\._U\)\({ID}\?(?P<config>{ID})\.J8:null,(?P<args>{ID})\),(?P<data>{ID})=(?P=query)\?\.data",
        module, "速度配置读取",
    )
    access = _one(rf"let\{{serviceTierAccess:(?P<access>{ID})\}}=\(0,{ID}\.U\)\({ID}\);", module, "速度权限")
    a, q, x = account.groupdict(), query.groupdict(), access.groupdict()
    patch = (
        access[0] + f"/*{HOOK_MARKER}*/"
        f"let __duoSpeedConfig=(0,{q['hook']}._U)({a['account']}?.authMethod===\"apikey\"?"
        f"{q['config']}.J8:null,{q['args']});"
        f"if({a['account']}?.authMethod===\"apikey\"){x['access']}="
        "__duoSpeedConfig?.data!=null&&__duoSpeedConfig.data.features?.fast_mode!==!1"
        "?__duoFastAccess:null;"
    )
    module = module[:access.start()] + patch + module[access.end():]
    module_start = module.index("{") + 1
    module = (module[:module_start] + "const __duoFastAccess=Object.freeze({fast:!0,ultrafast:!1,orbitApi:!0});"
              + module[module_start:])
    return text[:start] + module + text[end:]


def _request(text: str) -> str:
    # The two error handlers may be emitted in either order when the bundler
    # inlines a helper. Select the later anchor whose factory contains both.
    anchors = ("Failed to read service tier model", "Failed to read service tier for request")
    start, end = _module_start(text, anchors, (*anchors, "amazonBedrock", "includeLayers:!1"),
                               "速度请求模块")
    module = text[start:end]
    auth = _one(
        rf"async function(?: {ID})?\((?P<store>{ID}),(?P<host>{ID})\)\{{let (?P<auth>{ID})=await \(0,{ID}\.{ID}\)\((?P=store),(?P=host)\);"
        rf"(?=if\([\"'`]chatgpt[\"'`]!==(?P=auth)&&[\"'`]personalAccessToken[\"'`]!==(?P=auth)&&[\"'`]amazonBedrock[\"'`]!==(?P=auth)\)return null;)",
        module, "速度请求身份",
    )
    config = _one(
        rf"let\{{config:(?P<config>{ID})\}}=await \(0,(?P<reader>{ID})\.(?P<read>{ID})\)\({ID},{ID},\{{includeLayers:!1,cwd:null\}},\{{priority:[\"'`]critical[\"'`]\}}\),(?P<resolved>{ID})=\(0,(?P=reader)\.(?P<resolve>{ID})\)\((?P=config)\);",
        module, "速度请求配置读取",
    )
    a, c = auth.groupdict(), config.groupdict()
    patch = (
        auth[0] + f"/*{REQUEST_MARKER}*/"
        f"if({a['auth']}===\"apikey\"){{let {{config:__duoConfig}}=await (0,{c['reader']}.{c['read']})"
        f"({a['store']},{a['host']},{{includeLayers:!1,cwd:null}},{{priority:\"critical\"}}),"
        f"__duoResolved=(0,{c['reader']}.{c['resolve']})(__duoConfig);"
        "return __duoResolved.features?.fast_mode!==!1?{fast:!0,ultrafast:!1}:null}"
    )
    module = module[:auth.start()] + patch + module[auth.end():]
    return text[:start] + module + text[end:]


def discover_speed_request_patches(scripts: dict[str, bytes]) -> dict[str, bytes]:
    """Find and patch every loaded copy; fail closed when contracts drift."""
    result: dict[str, bytes] = {}
    hooks = requests = 0
    for name, body in scripts.items():
        has_hook = (b'serviceTierForRequest:' in body and b'setDefaultServiceTierConfig' in body
                    and b'updateThreadSettingsForNextTurn' in body and b'clearPrewarmedThreads():void 0' in body)
        has_request = (b'Failed to read service tier for request' in body
                       and b'Failed to read service tier model' in body and b'amazonBedrock' in body)
        if not has_hook and not has_request:
            continue
        text = body.decode('utf-8')
        if HOOK_MARKER in text or REQUEST_MARKER in text:
            raise PatchError('Codex 速度请求脚本已包含补丁；未更新实例。')
        if has_hook:
            text = _hook(text)
            hooks += 1
        if has_request:
            text = _request(text)
            requests += 1
        result[name] = text.encode('utf-8')
    if not hooks or not requests:
        raise PatchError(f'Codex 速度请求契约不完整（设置模块 {hooks}，请求模块 {requests}）；未更新实例。')
    return result
