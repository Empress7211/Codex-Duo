"""Locate renderer integrations by their contracts, not build filenames.

Every integration must have one unambiguous semantic match. A changed contract
stops preparation before any installed instance is replaced.
"""
from __future__ import annotations

import re


class PatchError(RuntimeError):
    pass


ID = r"[A-Za-z_$][\w$]*"
QUOTE = r"[\"'`]"
MARKERS = (
    "orbit-renderer-writer-release",
    "orbit-renderer-host-events",
    "orbit-renderer-resume",
    "orbit-renderer-input-gates",
)


def _one(pattern: str, text: str, contract: str) -> re.Match:
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise PatchError(f"Codex {contract}需唯一匹配，实际 {len(matches)} 处；未更新实例。")
    return matches[0]


def _replace(text: str, match: re.Match, replacement: str) -> str:
    return text[:match.start()] + replacement + text[match.end():]


def _named_function_start(text: str, before: int) -> re.Match:
    matches = list(re.finditer(rf"function\s+{ID}\((?P<first>{ID})(?:,[^)]*)?\)\{{", text[:before]))
    if not matches:
        raise PatchError("Codex 组件作用域无法定位；未更新实例。")
    return matches[-1]


RELEASE = (
    rf"let (?P<conversation>{ID})=(?P<normalize>(?:{ID}|\(0,{ID}\.{ID}\)))\((?P<thread>{ID})\);"
    rf"if\(!(?P<store>{ID})\.threadStore\.conversations\.has\((?P=conversation)\)\)"
    rf"\{{let {ID}=(?P=store)\.threadStore\.recordThreadRuntimeStatusEvidence"
    rf"\((?P=thread),(?P<status>{ID})\);"
)


def _patch_release(text: str) -> str:
    match = _one(RELEASE, text, "会话写入权释放接入点")
    v = match.groupdict()
    previous_case = text.rfind("case", 0, match.start())
    prefix = text[previous_case:match.start()]
    payload = _one(
        rf"let\{{threadId:{re.escape(v['thread'])},status:{re.escape(v['status'])}\}}=(?P<payload>{ID})\.params;",
        prefix, "会话状态通知参数",
    )["payload"]
    manager = _one(
        rf"(?P<manager>{ID})\.updateConversationState\({re.escape(v['conversation'])},",
        text[match.end():match.end() + 650], "会话状态管理器",
    )["manager"]
    # These are the same manager contracts used by its native reconnect path.
    if ".inactiveThreadUnsubscriber" not in text or "clearConversationStreamOwnership(" not in text:
        raise PatchError("Codex 缺少会话流所有权释放能力；未更新实例。")
    seam = f"let {v['conversation']}={v['normalize']}({v['thread']});"
    patch = (
        seam + "/*orbit-renderer-writer-release*/"
        f"if({payload}.params.orbitReleased===!0){{"
        f"{manager}.inactiveThreadUnsubscriber.clearConversationStreamOwnership({v['conversation']}),"
        f"{manager}.updateConversationState({v['conversation']},orbitState=>{{orbitState.resumeState=`needs_resume`}});"
        "window.dispatchEvent(new CustomEvent('orbit-writer-locks-changed',"
        f"{{detail:{{threadId:{v['thread']},has_writer:!1}}}}))}};"
    )
    return _replace(text, match, patch + match[0][len(seam):])


HOST_EVENTS = rf"case{QUOTE}workspace-root-options-updated{QUOTE}:"


def _patch_host_events(text: str) -> str:
    match = _one(HOST_EVENTS, text, "窗口状态事件入口")
    # The immediately preceding switch may be a nested, already closed
    # switch. The neighboring global-state case consumes the host event's
    # keys, giving us the outer payload without guessing at JS brace depth.
    global_case = _one(rf"case{QUOTE}global-state-updated{QUOTE}:", text, "全局状态事件入口")
    if global_case.start() < match.end():
        raise PatchError("Codex 窗口状态事件顺序已变化；未更新实例。")
    following_case = text.find("case", global_case.end())
    global_body = text[global_case.end():following_case if following_case >= 0 else len(text)]
    payloads = set(re.findall(rf"(?<![\w$.])({ID})\.keys\b", global_body))
    if len(payloads) != 1:
        raise PatchError("Codex 窗口状态事件参数无法定位；未更新实例。")
    payload = payloads.pop()
    if not re.search(rf"switch\({re.escape(payload)}\.type\)\{{", text[:match.start()]):
        raise PatchError("Codex 窗口状态事件参数无法定位；未更新实例。")
    next_case = text.find("case", match.end())
    case_body = text[match.end():next_case]
    end = _one(rf"break(?: {ID})?;", case_body, "窗口状态事件退出点")[0]
    insertion = "/*orbit-renderer-host-events*/"
    for event in ("orbit-writer-locks-changed", "orbit-writer-handoff-available"):
        insertion += (
            f"case'{event}':window.dispatchEvent(new CustomEvent('{event}',"
            f"{{detail:{payload}}}));{end}"
        )
    return _replace(text, match, insertion + match[0])


RESUME_RESULT = rf"isWriterConflict:(?P<conflictResult>{ID}),retryResume:(?P<retry>{ID})"


def _patch_resume(text: str) -> str:
    result = _one(RESUME_RESULT, text, "会话恢复结果契约")
    function = _named_function_start(text, result.start())
    thread = function["first"]
    function_body = text[function.start():result.start()]
    result_start = text.rfind("{", function.end(), result.start())
    if result_start < 0:
        raise PatchError("Codex 会话恢复结果作用域无法定位；未更新实例。")
    loading_result = _one(rf"isResuming:(?P<result>{ID}),",
                          text[result_start:result.start()], "会话恢复加载结果")["result"]
    loading = _one(
        rf"(?<![\w$]){re.escape(loading_result)}=(?P<wanted>{ID})&&(?P<loading>{ID})(?=[,;])",
        function_body, "会话恢复加载结果绑定",
    )
    v = loading.groupdict()
    state = _one(
        rf"\[{re.escape(v['loading'])},(?P<setLoading>{ID})\]="
        rf"\(0,(?P<react>{ID})\.useState\)\("
        rf"(?P<initial>{re.escape(v['wanted'])}(?:&&null==(?P<error>{ID}))?)\)",
        function_body, "会话恢复加载状态",
    )
    v.update(state.groupdict())
    conflict = _one(
        rf"(?<![\w$]){re.escape(result['conflictResult'])}={re.escape(v['wanted'])}&&"
        rf"(?:{re.escape(thread)}!=null|null!={re.escape(thread)})&&"
        rf"(?P<conflict>{ID})==={re.escape(thread)}",
        function_body, "会话写入权冲突状态",
    )["conflict"]
    return_position = text.rfind(";return ", function.end(), result.start())
    if return_position < 0:
        raise PatchError("Codex 会话恢复 hook 返回入口无法定位；未更新实例。")
    # Only request a native retry after the owner has released its real lock or
    # announced a possible handoff; the backend still performs the final gate.
    patch = (
        ";/*orbit-renderer-resume*/"
        f"(0,{v['react']}.useLayoutEffect)(()=>{{{v['setLoading']}({v['initial']})}},"
        f"[{thread},{v['wanted']}{',' + v['error'] if v['error'] else ''}]);"
        f"(0,{v['react']}.useEffect)(()=>{{let orbitWriterEvent=orbitEvent=>{{"
        f"orbitEvent.detail?.threadId==={thread}&&{conflict}==={thread}&&"
        "(orbitEvent.detail?.has_writer===!1||orbitEvent.type==='orbit-writer-handoff-available'&&"
        "orbitEvent.detail?.handoff_available===!0)&&"
        f"{result['retry']}()}};"
        "window.addEventListener('orbit-writer-locks-changed',orbitWriterEvent);"
        "window.addEventListener('orbit-writer-handoff-available',orbitWriterEvent);"
        "return()=>{window.removeEventListener('orbit-writer-locks-changed',orbitWriterEvent);"
        "window.removeEventListener('orbit-writer-handoff-available',orbitWriterEvent)}"
        f"}},[{thread},{conflict},{result['retry']}]);return "
    )
    text = text[:return_position] + patch + text[return_position + len(";return "):]
    message = _one(
        rf"id:{QUOTE}localTaskRow\.resumeLiveWriterError{QUOTE},defaultMessage:(?P<quote>{QUOTE})(?P<message>[^`\"']*)(?P=quote),",
        text, "会话占用提示",
    )
    formatter = _named_function_start(text, message.start())["first"]
    return _replace(text, message,
        "id:`orbit.resumeWriterHeld`,defaultMessage:"
        + formatter + ".locale.toLowerCase().startsWith(`zh`)?"
        "`另一窗口仍占有此会话的写入权。{br}任务可能已经空闲；等待写入权释放后重试。`:"
        "`Another window still holds this chat's writer.{br}The task may be idle; retry after the writer is released.`,")


INPUT_COMPONENT = rf"id:{QUOTE}localConversation\.writerConflict\.title{QUOTE}"


def _patch_input_gates(text: str) -> str:
    # A resuming chat is read-only before the first request returns, even when
    # the native component has not yet received a writer-conflict response.
    simple = _one(
        rf"let (?P<readonly>{ID})=!(?P<allowed>{ID})\|\|(?P<conflict>{ID}),"
        rf"(?P<resuming>{ID})=(?P<loading>{ID})&&!(?P=conflict),"
        rf"(?P<show>{ID})=(?P=allowed)&&!(?P=conflict),(?P<next>{ID});",
        text, "嵌入会话输入保护",
    )
    v = simple.groupdict()
    text = _replace(text, simple,
        f"let {v['readonly']}=!{v['allowed']}||{v['conflict']}||{v['loading']},"
        f"{v['resuming']}={v['loading']}&&!{v['conflict']},"
        f"{v['show']}={v['allowed']}&&!{v['conflict']}&&!{v['loading']},{v['next']};")
    outer = _one(
        rf"let (?P<surface>{ID})={ID}\.visibleSurface!=null,"
        rf"(?P<history>{ID})={ID}==null\|\|(?P<loading>{ID})&&!(?P<conflict>{ID}),"
        rf"(?P<readonly>{ID})=(?P<readonlyExpr>{ID}(?:\|\|{ID})+),"
        rf"(?P<resuming>{ID})=(?P=loading)&&!(?P=conflict),(?P<next>{ID});",
        text, "主会话输入保护",
    )
    v = outer.groupdict()
    if v["conflict"] not in v["readonlyExpr"].split("||"):
        raise PatchError("Codex 主会话未绑定写入权状态；未更新实例。")
    replacement = outer[0].replace(
        f"{v['readonly']}={v['readonlyExpr']},",
        f"{v['readonly']}={v['readonlyExpr']}||{v['loading']},", 1)
    text = _replace(text, outer, replacement)
    show = _one(
        rf"let (?P<show>{ID})=(?P<ready>{ID})&&!(?P<hydrating>{ID})&&!{re.escape(v['conflict'])},(?P<next>{ID});",
        text, "主会话编辑区显示条件",
    )
    text = _replace(text, show,
        show[0].replace(f"&&!{v['conflict']},", f"&&!{v['conflict']}&&!{v['loading']},", 1))
    submit = _one(
        rf"submitDisabled:(?P<disabled>{ID}\|\|{ID}\|\|(?P<handoff>{ID})),"
        rf"isThreadHandoffInProgress:(?P=handoff)",
        text, "消息发送保护",
    )
    function = _named_function_start(text, submit.start())
    declaration = text[function.end():text.find("}=", function.end())]
    history = _one(rf"isThreadHistoryLoading:(?P<history>{ID})", declaration, "消息发送加载参数")["history"]
    text = _replace(text, submit,
        f"submitDisabled:{submit['disabled']}||{history},isThreadHandoffInProgress:{submit['handoff']}")
    return _patch_input_messages(text)


def _patch_input_messages(text: str) -> str:
    # Native message ids would select their old 'active elsewhere' translation.
    # Distinct ids keep ownership wording accurate without altering language packs.
    locale = "(document.documentElement.lang||navigator.language).toLowerCase().startsWith(`zh`)"
    for key, chinese, english in (
        ("title", "另一窗口占有此会话的写入权", "Another window holds this chat's writer"),
        ("description", "任务可能已经空闲，但后端仍占有写入权。等待释放后重试。",
         "The task may be idle, but its backend still holds the writer. Retry after it releases the chat."),
    ):
        message = _one(
            rf"id:{QUOTE}localConversation\.writerConflict\.{key}{QUOTE},defaultMessage:(?P<quote>{QUOTE})(?P<message>[^`\"']*)(?P=quote),",
            text, "写入权提示 " + key,
        )
        text = _replace(text, message,
            f"id:`orbit.writerHeld.{key}`,defaultMessage:{locale}?`{chinese}`:`{english}`,")
    return "/*orbit-renderer-input-gates*/" + text


def _patch_numeric_input_gates(text: str) -> str:
    """Apply the same recovery gates to the compiled local conversation UI."""
    if re.search(rf"isWriterConflict:{ID},providerBlock:{ID},isReadyForInteraction:{ID}", text):
        return _patch_provider_input_gates(text)
    hooks = list(re.finditer(
        rf"isResuming:(?P<loading>{ID}),hasResumeFailed:{ID},isUnavailable:{ID},"
        rf"hasReadOnlyAccess:(?P<access>{ID}),isWriterConflict:(?P<conflict>{ID}),"
        rf"environmentStatus:{ID},retryResume:{ID}", text))
    if len(hooks) != 2:
        raise PatchError("Codex 本地会话恢复入口需唯一覆盖两个界面；未更新实例。")
    embed, main = hooks
    v = embed.groupdict()
    simple = _one(
        rf"let (?P<readonly>{ID})=!(?P<allowed>{ID})\|\|{re.escape(v['conflict'])}\|\|{re.escape(v['access'])},"
        rf"(?P<resuming>{ID})={re.escape(v['loading'])}&&!{re.escape(v['conflict'])},"
        rf"(?P<show>{ID})=(?P=allowed)&&!{re.escape(v['conflict'])}&&!{re.escape(v['access'])};",
        text[embed.end():], "嵌入会话输入保护")
    pos = embed.end() + simple.start()
    simple_end = embed.end() + simple.end()
    sv = simple.groupdict()
    replacement = (f"let {sv['readonly']}=!{sv['allowed']}||{v['conflict']}||{v['access']}||{v['loading']},"
                   f"{sv['resuming']}={v['loading']}&&!{v['conflict']},"
                   f"{sv['show']}={sv['allowed']}&&!{v['conflict']}&&!{v['access']}&&!{v['loading']};")
    text = text[:pos] + replacement + text[simple_end:]

    # Re-find the main hook after the first insertion shifted offsets.
    main_hooks = list(re.finditer(
        rf"isResuming:(?P<loading>{ID}),hasResumeFailed:{ID},isUnavailable:{ID},"
        rf"hasReadOnlyAccess:(?P<access>{ID}),isWriterConflict:(?P<conflict>{ID}),"
        rf"environmentStatus:{ID},retryResume:{ID}", text))
    main = main_hooks[1]
    v = main.groupdict()
    tail = text[main.end():]
    readonly = _one(
        rf"let {ID}={ID},(?P<readonly>{ID})=(?P<expr>{ID}(?:\|\|{ID})*\|\|"
        rf"{re.escape(v['conflict'])}\|\|!0==={re.escape(v['access'])});",
        tail, "主会话输入保护")
    show = _one(
        rf"let (?P<show>{ID})=(?P<ready>{ID})&&!(?P<blocked>{ID})&&!{re.escape(v['conflict'])}"
        rf"&&!{re.escape(v['access'])};", tail, "主会话编辑区显示条件")
    for match in sorted((readonly, show), key=lambda m: m.start(), reverse=True):
        value = match[0][:-1] + (f"||{v['loading']};" if match is readonly else f"&&!{v['loading']};")
        start, end = main.end() + match.start(), main.end() + match.end()
        text = text[:start] + value + text[end:]

    submit = _one(
        rf"submitDisabled:(?P<disabled>{ID}\|\|{ID}\|\|(?P<handoff>{ID})),"
        rf"isThreadHandoffInProgress:(?P=handoff)", text, "消息发送保护")
    function = _named_function_start(text, submit.start())
    declaration = text[function.end():text.find("}=", function.end())]
    history = _one(rf"isThreadHistoryLoading:(?P<history>{ID})", declaration, "消息发送加载参数")["history"]
    text = _replace(text, submit,
        f"submitDisabled:{submit['disabled']}||{history},isThreadHandoffInProgress:{submit['handoff']}")
    return _patch_input_messages(text)


def _patch_provider_input_gates(text: str) -> str:
    hooks = list(re.finditer(
        rf"isResuming:(?P<loading>{ID}),hasResumeFailed:{ID},isUnavailable:{ID},"
        rf"hasReadOnlyAccess:(?P<access>{ID}),isWriterConflict:(?P<conflict>{ID}),"
        rf"providerBlock:(?P<provider>{ID}),isReadyForInteraction:(?P<ready>{ID}),"
        rf"environmentStatus:{ID}(?:,retryResume:{ID})?", text))
    if len(hooks) != 2:
        raise PatchError("Codex 本地会话恢复入口需唯一覆盖两个界面；未更新实例。")
    embed, main = hooks
    v = embed.groupdict()
    simple = _one(
        rf"let (?P<readonly>{ID})=!(?P<allowed>{ID})\|\|{re.escape(v['access'])}\|\|"
        rf"{re.escape(v['conflict'])}\|\|!{re.escape(v['ready'])},"
        rf"(?P<resuming>{ID})={re.escape(v['loading'])}&&!{re.escape(v['conflict'])}&&null=={re.escape(v['provider'])},"
        rf"(?P<show>{ID})=(?P=allowed)&&!{re.escape(v['access'])}&&!{re.escape(v['conflict'])}&&null=={re.escape(v['provider'])};",
        text[embed.end():main.start()], "嵌入会话输入保护")
    replacement = simple[0].replace(f"||!{v['ready']},", f"||!{v['ready']}||{v['loading']},", 1)
    replacement = replacement[:-1] + f"&&!{v['loading']};"
    start, end = embed.end() + simple.start(), embed.end() + simple.end()
    text = text[:start] + replacement + text[end:]

    # Re-find after insertion so each match remains scoped to the main view.
    main = list(re.finditer(
        rf"isResuming:(?P<loading>{ID}),hasResumeFailed:{ID},isUnavailable:{ID},"
        rf"hasReadOnlyAccess:(?P<access>{ID}),isWriterConflict:(?P<conflict>{ID}),"
        rf"providerBlock:(?P<provider>{ID}),isReadyForInteraction:(?P<ready>{ID}),environmentStatus:{ID}", text))[1]
    v = main.groupdict()
    tail = text[main.end():]
    readonly = _one(rf"(?P<readonly>{ID})={ID}\|\|!{re.escape(v['ready'])},"
                    rf"{ID}={re.escape(v['loading'])}&&!{re.escape(v['conflict'])}&&null=={re.escape(v['provider'])},"
                    rf"(?P<submit>{ID})=!{re.escape(v['ready'])};", tail, "主会话输入保护")
    show = _one(rf"let {ID}={ID}&&!{ID}&&!{re.escape(v['conflict'])}&&!{re.escape(v['access'])}"
                rf"&&null=={re.escape(v['provider'])};", tail, "主会话编辑区显示条件")
    for match in sorted((readonly, show), key=lambda m: m.start(), reverse=True):
        value = match[0]
        if match is readonly:
            value = value.replace(f"{v['ready']},", f"{v['ready']}||{v['loading']},", 1)
            value = value[:-1] + f"||{v['loading']};"
        else:
            value = value[:-1] + f"&&!{v['loading']};"
        start, end = main.end() + match.start(), main.end() + match.end()
        text = text[:start] + value + text[end:]

    submit = _one(rf"submitDisabled:(?P<disabled>{ID}\|\|{ID}\|\|(?P<handoff>{ID})),"
                  rf"isThreadHandoffInProgress:(?P=handoff)", text, "消息发送保护")
    function = _named_function_start(text, submit.start())
    declaration = text[function.end():text.find("}=", function.end())]
    history = _one(rf"isThreadHistoryLoading:(?P<history>{ID})", declaration, "消息发送加载参数")["history"]
    text = _replace(text, submit, f"submitDisabled:{submit['disabled']}||{history},"
                    f"isThreadHandoffInProgress:{submit['handoff']}")
    return _patch_input_messages(text)


def _patch_numeric_global_input(text: str) -> str:
    hook = _one(
        rf"isResuming:(?P<loading>{ID}),hasResumeFailed:(?P<failed>{ID}),"
        rf"isUnavailable:(?P<unavailable>{ID}),isWriterConflict:(?P<conflict>{ID}),"
        rf"hasReadOnlyAccess:(?P<access>{ID}),retryResume:{ID}", text, "全局入口会话恢复")
    v = hook.groupdict()
    gate = _one(
        rf"isThreadHistoryLoading:{re.escape(v['loading'])}\|\|[^,;]+,"
        rf"submitDisabled:{re.escape(v['failed'])}\|\|{re.escape(v['unavailable'])}\|\|"
        rf"{re.escape(v['conflict'])}\|\|{re.escape(v['access'])}",
        text[hook.end():], "全局入口消息发送保护")
    start, end = hook.end() + gate.start(), hook.end() + gate.end()
    text = text[:start] + gate[0] + f"||{v['loading']}" + text[end:]
    return "/*orbit-renderer-global-input-gate*/" + text


def discover_renderer_patches(scripts: dict[str, bytes]) -> dict[str, bytes]:
    """Return only changed renderer files, or fail before creating a copy."""
    texts = {name: body.decode("utf-8") for name, body in scripts.items()}
    if any(re.search(r"/async/\d+\.[0-9a-f]+\.js$", name) for name in texts):
        return _discover_numeric_renderer_patches(texts)
    patches = {}
    groups = (
        (RELEASE, _patch_release, "会话写入权释放"),
        (HOST_EVENTS, _patch_host_events, "窗口状态事件"),
        (RESUME_RESULT, _patch_resume, "会话恢复"),
        (INPUT_COMPONENT, _patch_input_gates, "消息输入"),
    )
    for pattern, patcher, contract in groups:
        matches = [name for name, text in texts.items() if re.search(pattern, text)]
        if len(matches) != 1:
            raise PatchError(f"Codex {contract}脚本需唯一匹配，实际 {len(matches)} 个；未更新实例。")
        name = matches[0]
        text = patcher(texts[name])
        texts[name] = text
        patches[name] = text.encode("utf-8")
    return patches


def _discover_numeric_renderer_patches(texts: dict[str, str]) -> dict[str, bytes]:
    patches = {}

    def one_name(names: list[str], description: str) -> str:
        if len(names) != 1:
            raise PatchError(f"Codex {description}需唯一匹配，实际 {len(names)} 个；未更新实例。")
        return names[0]

    def apply(name: str, patcher) -> None:
        texts[name] = patcher(texts[name])
        patches[name] = texts[name].encode("utf-8")

    for pattern, patcher, description in (
        (RELEASE, _patch_release, "会话写入权释放"),
        (HOST_EVENTS, _patch_host_events, "窗口状态事件"),
    ):
        names = [name for name, text in texts.items() if name.startswith("webview/") and re.search(pattern, text)]
        apply(one_name(names, description + "脚本"), patcher)

    resumes = [name for name, text in texts.items() if name.startswith("webview/")
               and "resumeLiveWriterError" in text and re.search(RESUME_RESULT, text)]
    if not resumes:
        raise PatchError("Codex 会话恢复脚本无法定位；未更新实例。")
    for name in resumes:
        apply(name, _patch_resume)

    local = [name for name in resumes if re.search(INPUT_COMPONENT, texts[name])]
    apply(one_name(local, "本地会话输入脚本"), _patch_numeric_input_gates)
    global_input = [name for name, text in texts.items() if name.startswith("webview/")
                    and "isThreadHistoryLoading:" in text and "submitDisabled:" in text
                    and re.search(rf"isResuming:{ID},hasResumeFailed:{ID},isUnavailable:{ID},"
                                  rf"isWriterConflict:{ID},hasReadOnlyAccess:{ID},retryResume:{ID}", text)]
    apply(one_name(global_input, "全局入口输入脚本"), _patch_numeric_global_input)
    return patches
