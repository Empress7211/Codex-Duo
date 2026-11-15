"""Enable the native Fast selector for API-key hosts in managed Codex copies.

Only the selector's settings are changed; the shared account entitlement hook
and subscription UI remain untouched. The model catalog still decides whether
Fast can be selected. API labels avoid claiming a provider's speed or price.
"""
from __future__ import annotations

import re

from desktop_renderer_patches import ID, PatchError, _one, _replace

MARKER = 'orbit-api-speed-ui'


def is_speed_ui_script(body: bytes) -> bool:
    return ((b'serviceTierAccess:' in body and b'availableOptions:' in body and
             b'activeProfileForWrite:' in body and b'authMethod' in body) or
            (b'composer.openModelPicker' in body and b'.availableOptions.length>1' in body) or
            (b'composer.intelligencePicker.fastMode.speed.tooltip.withMultiplier' in body and
             b'composer.intelligencePicker.fastMode.advanced.subtitle.withMultiplier' in body and
             b'KeyboardAnnouncement' in body and b'FastModeToggle' in body and b'serviceTierOptions' in body))


def _patch_settings(text: str) -> str:
    # The request patch adds a marker only to API access.  Keep the native
    # model catalogue filtering and alter labels only for that marked access.
    if 'orbit-api-speed-request-hook' not in text:
        raise PatchError('Codex API 速度请求补丁缺失；未更新实例。')
    access = _one(
        rf'let\{{serviceTierAccess:(?P<access>{ID})\}}=\(0,{ID}\.{ID}\)\({ID}\);',
        text, 'API 速度设置接入点',
    )
    context = text[access.end():access.end() + 10000]
    option = _one(
        rf'(?P<callback>{ID})=(?P<param>{ID})=>\(0,{ID}\.{ID}\)'
        rf'\((?P=param)\.value,{re.escape(access["access"])}\)'
        rf'(?P<between>.{{0,150}}?)'
        rf'(?P<target>{ID})=\(0,{ID}\.{ID}\)\({ID}\)\.filter\((?P=callback)\)',
        context, 'API 速度模型档位',
    )
    start, end = access.end() + option.start(), access.end() + option.end()
    # The official label asserts a specific multiplier and usage premium.
    # Neither is known for third-party APIs, so use provider-neutral wording.
    replacement = option[0] + (
        f'.map(orbitOption=>{access["access"]}?.orbitApi?'
        '(orbitOption.value==null?{...orbitOption,orbitApi:!0,'
        'label:{id:"codexDuo.apiDefaultLabel",defaultMessage:(navigator.language||"").toLowerCase().startsWith("zh")?"默认":"Default"},'
        'description:{id:"codexDuo.apiDefaultDescription",defaultMessage:(navigator.language||"").toLowerCase().startsWith("zh")?"不指定处理档位":"No processing tier specified"}}:'
        '{...orbitOption,orbitApi:!0,speedMultiplier:null,description:{'
        'id:"codexDuo.apiFastDescription",'
        'defaultMessage:(navigator.language||"").toLowerCase().startsWith("zh")?'
        '"请求 Fast 档位；是否生效及费用取决于供应商":'
        '"Requests Fast; availability and pricing depend on the provider",'
        'description:"API speed tier request"}}):orbitOption)'
    )
    return text[:start] + replacement + text[end:]


def _patch_composer(text: str) -> str:
    # Settings hook already yields no options for ineligible logins. Remove
    # only this redundant second entitlement gate, preserving all other gates.
    match = _one(
        rf'null!=(?P<access>{ID})&&(?P<settings>{ID})\.availableOptions\.length>1',
        text, '输入框速度显示入口',
    )
    prefix = text[max(0, match.start() - 10000):match.start()]
    if not re.search(rf'\{{serviceTierAccess:{re.escape(match["access"])}\}}=', prefix):
        raise PatchError('Codex 输入框速度权限作用域无法定位；未更新实例。')
    return _replace(text, match,
                    f'/*{MARKER}*/{match["settings"]}.availableOptions.length>1')


def _patch_speed_controls(text: str) -> str:
    # The advanced menu rewrites the option's description. Preserve the API
    # description there, while leaving the native subscription copy intact.
    advanced = _one(
        rf'(?P<param>{ID})=>["\'`]fast["\'`]===(?P=param)\.iconKind\?'
        rf'(?=\{{\.\.\.(?P=param),label:.*?composer\.intelligencePicker\.fastMode\.advanced\.title)',
        text, '高级速度菜单说明',
    )
    text = _replace(text, advanced, advanced[0][:-1] + f'&&!{advanced["param"]}.orbitApi?')
    tip = _one(r'id:["\'`]composer\.intelligencePicker\.fastMode\.speed\.tooltip\.withMultiplier["\'`]', text, '速度按钮提示')
    start = text.rfind('tooltipText:', 0, tip.start())
    end = text.find(',variant:', tip.end())
    if start < 0 or end < 0 or end - start > 1500:
        raise PatchError('Codex 速度按钮提示范围无法定位；未更新实例。')
    label = _one(rf'(?P<prefix>[\"\'`]?aria-label[\"\'`]?:)(?P<label>{ID}),className:[^,]+FastModeToggle',
                 text[max(0, start-1000):start], '速度按钮标签')
    label_start = max(0, start-1000) + label.start()
    label_end = max(0, start-1000) + label.end()
    # Derive the original checkbox state and Fast option from this same control.
    control = _one(rf'[\"\'`]?aria-checked[\"\'`]?:\s*(?P<checked>{ID}),[\"\'`]?aria-describedby[\"\'`]?:\s*(?P<description>{ID}),',
                 text[max(0, start-1000):start], '速度按钮状态')
    state = control['checked']
    original = text[start + len('tooltipText:'):end]
    source = _one(rf'values:\{{speedMultiplier:(?P<option>{ID})\.speedMultiplier\?\?1\.5\}}', original, '速度按钮参数')
    jsx = _one(rf'\(0,(?P<runtime>{ID})\.jsxs\)', original, '速度提示渲染')['runtime']
    neutral = ('(navigator.language||" ").toLowerCase().startsWith("zh")?'
               '"请求 Fast 档位；是否生效及费用取决于供应商":'
               '"Requests Fast; availability and pricing depend on the provider"')
    replacement = (f'tooltipText:{source["option"]}.orbitApi?'
                   f'(0,{jsx}.jsx)("span",{{children:{neutral}}}):{original}')
    text = text[:start] + replacement + text[end:]
    # The compact control also supplies its native usage warning to screen
    # readers; do not present subscription accounting for API providers.
    # Associate the announcement by aria-describedby, rather than assuming a
    # particular JSX helper or a single identifier child. Bundler versions may
    # render the same announcement with jsx or jsxs and an array of children.
    announcement_start = start + len(replacement)
    key = _one(
        rf'\(0,{jsx}\.jsxs?\)\([\s\S]{{0,150}}?KeyboardAnnouncement,'
        rf'id:{re.escape(control["description"])},children:(?P<warning>{ID}|\[[^\[\]]*\])\}}\)',
        text[announcement_start:announcement_start + 2000], '速度按钮无障碍提示',
    )
    absolute_start, absolute_end = announcement_start + key.start(), announcement_start + key.end()
    updated = key[0].replace(f'children:{key["warning"]}',
                             f'children:{source["option"]}.orbitApi?null:{key["warning"]}')
    text = text[:absolute_start] + updated + text[absolute_end:]
    replacement_label = label[0].replace(
        f'{label["prefix"]}{label["label"]}',
        f'{label["prefix"]}{source["option"]}.orbitApi?'
        f'({state}?"Cancel Fast request":"Request Fast tier"):{label["label"]}')
    return text[:label_start] + replacement_label + text[label_end:]


def discover_speed_ui_patches(scripts: dict[str, bytes]) -> dict[str, bytes]:
    patches = {}
    settings = composer = controls = 0
    for name, body in scripts.items():
        if not is_speed_ui_script(body):
            continue
        text = body.decode('utf-8')
        if 'activeProfileForWrite:' in text and 'serviceTierAccess:' in text and 'availableOptions:' in text:
            text = _patch_settings(text)
            settings += 1
        if 'composer.openModelPicker' in text and '.availableOptions.length>1' in text:
            text = _patch_composer(text)
            composer += 1
        if ('composer.intelligencePicker.fastMode.speed.tooltip.withMultiplier' in text and
                'composer.intelligencePicker.fastMode.advanced.subtitle.withMultiplier' in text and
                'KeyboardAnnouncement' in text and 'FastModeToggle' in text and 'serviceTierOptions' in text):
            text = _patch_speed_controls(text)
            controls += 1
        patches[name] = text.encode('utf-8')
    if not settings or not composer or not controls:
        raise PatchError('Codex 未提供完整的 API 速度设置与输入框接口；未更新实例。')
    return patches
