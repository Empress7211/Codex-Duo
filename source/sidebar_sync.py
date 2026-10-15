"""Coordinate managed sidebars while treating the official sidebar as read-only.

The first snapshot is the activation API: it imports the official sidebar and
admits only unambiguous additions from saved managed sidebars. Later requests
apply only official changes since the stored baseline, then the requesting UI's
before/after delta. The helper never writes any home's global state or imports
account/cloud caches.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile

if __name__ == '__main__':
    sys.dont_write_bytecode = True

from sidebar_store import GLOBAL_STATE, PROJECT_MAPPING, TOP_LEVEL_KEYS


KEYS = tuple(sorted(TOP_LEVEL_KEYS | {PROJECT_MAPPING}))
LIST_KEYS = frozenset({"project-order", "projectless-thread-ids",
                       "pinned-thread-ids", "pinned-project-ids"})
ORDER_MAP_KEYS = frozenset({"sidebar-project-thread-orders"})
MISSING = object()


class SidebarSyncError(ValueError):
    def __init__(self, message: str, conflicts: list[str] | None = None):
        super().__init__(message)
        self.conflicts = conflicts or []


def _read_object(path: Path, *, missing: bool = False) -> dict:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        if missing:
            return {}
        raise SidebarSyncError(f"侧栏同步文件不存在：{path.name}") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SidebarSyncError(f"侧栏同步文件 JSON 格式错误：{path.name}") from None
    if not isinstance(value, dict):
        raise SidebarSyncError(f"侧栏同步文件必须为 JSON 对象：{path.name}")
    return value


def _owned_path(root: Path, path: Path) -> None:
    if not path.resolve().is_relative_to(root):
        raise SidebarSyncError(f"侧栏同步路径超出工具目录：{path.name}")


def _context(root: Path, source_home: Path, profile_home: Path) -> tuple[Path, Path, Path, list[Path]]:
    root = Path(root).expanduser().resolve()
    source = Path(source_home).expanduser().resolve()
    profile = Path(profile_home).expanduser().absolute()
    if root.is_relative_to(source) or source.is_relative_to(root):
        raise SidebarSyncError("工具目录不得与原 Codex 目录重叠。")
    manifest_path = root / "manifest.json"
    _owned_path(root, manifest_path)
    manifest = _read_object(manifest_path)
    if manifest.get("owner") != "codex-dual" or manifest.get("root") != str(root):
        raise SidebarSyncError("工具目录所有权标识不匹配。")
    sync = manifest.get("sync", {})
    if not isinstance(sync, dict) or (sync and sync.get("source_home") != str(source)):
        raise SidebarSyncError("侧栏同步的原 Codex 目录与声明不匹配。")
    profiles = manifest.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise SidebarSyncError("受管实例清单格式错误。")
    homes = []
    seen = set()
    for item in profiles:
        identifier = item.get("id") if isinstance(item, dict) else None
        if (not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", identifier)
                or identifier in seen):
            raise SidebarSyncError("受管实例清单包含无效或重复标识。")
        seen.add(identifier)
        home = root / "profiles" / identifier / "codex-home"
        for path in (home.parent, home, home / GLOBAL_STATE):
            _owned_path(root, path)
        if not home.is_dir() or home.is_symlink():
            raise SidebarSyncError("受管实例 codex-home 不存在或不是独立目录。")
        homes.append(home)
    if profile not in homes:
        raise SidebarSyncError("侧栏请求必须来自清单中的受管实例。")
    shared = root / "shared"
    _owned_path(root, shared)
    for name in ("sidebar-state.json", "sidebar-state.lock"):
        path = shared / name
        _owned_path(root, path)
        if path.is_symlink():
            raise SidebarSyncError("侧栏状态和锁文件不得为符号链接。")
    return root, source, profile, homes


def _validate_value(key: str, value) -> None:
    expected = list if key in LIST_KEYS else dict
    if not isinstance(value, expected):
        raise SidebarSyncError(f"侧栏字段结构错误：{key}")
    if expected is list and not all(isinstance(item, str) for item in value):
        raise SidebarSyncError(f"侧栏顺序字段必须为标识列表：{key}")
    if key == PROJECT_MAPPING:
        if any(not isinstance(mapping, dict)
               or not all(isinstance(legacy, str) and isinstance(server, str)
                          for legacy, server in mapping.items())
               for mapping in value.values()):
            raise SidebarSyncError("本地项目标识映射结构错误。")


def _canonical_value(key: str, value, home: Path, source: Path):
    host = "local:" + str(home)
    canonical_host = "local:" + str(source)
    if key == PROJECT_MAPPING:
        if not isinstance(value, dict):
            raise SidebarSyncError("本地项目标识映射结构错误。")
        local = {canonical_host: copy.deepcopy(value[host])} if host in value else {}
        _validate_value(key, local)
        return local
    _validate_value(key, value)
    if key == "thread-project-membership-host-ids":
        return {identifier: canonical_host if item == host else copy.deepcopy(item)
                for identifier, item in value.items()}
    return copy.deepcopy(value)


def _canonical_state(value: dict, home: Path, source: Path) -> dict:
    return {key: _canonical_value(key, value[key], home, source)
            for key in KEYS if key in value}


def _project(state: dict, source: Path, profile: Path) -> dict:
    canonical_host = "local:" + str(source)
    host = "local:" + str(profile)
    result = copy.deepcopy(state)
    if PROJECT_MAPPING in result:
        mapping = result[PROJECT_MAPPING]
        result[PROJECT_MAPPING] = {host: mapping[canonical_host]} if canonical_host in mapping else {}
    key = "thread-project-membership-host-ids"
    if key in result:
        result[key] = {identifier: host if item == canonical_host else item
                       for identifier, item in result[key].items()}
    return result


def _equal(first, second) -> bool:
    if first is MISSING or second is MISSING:
        return first is second
    return first == second


def _identity(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _list_delta(current: list, before: list, after: list) -> list:
    before_ids = {_identity(value) for value in before}
    after_ids = {_identity(value) for value in after}
    removed = before_ids - after_ids
    added = after_ids - before_ids
    surviving = {_identity(value): value for value in current if _identity(value) not in removed}
    # Unchanged IDs in a stale request cannot revive a peer's deletion. The
    # request orders available IDs; concurrent peer additions stay at the end.
    result = []
    seen = set()
    for value in after:
        identity = _identity(value)
        if identity not in seen and (identity in surviving or identity in added):
            result.append(copy.deepcopy(value))
            seen.add(identity)
    for value in current:
        identity = _identity(value)
        if identity in surviving and identity not in seen:
            result.append(copy.deepcopy(value))
            seen.add(identity)
    return result


def _delta(current, before, after, *, collection_depth: int = 0):
    if _equal(before, after):
        return current
    if after is MISSING:
        # Removing a whole map/list only removes records/IDs the UI observed.
        # A project record's deletion, in contrast, removes that entire record.
        if collection_depth and isinstance(before, dict) and isinstance(current, dict):
            remaining = copy.deepcopy(current)
            for key, old_value in before.items():
                value = _delta(current.get(key, MISSING), old_value, MISSING,
                               collection_depth=collection_depth - 1)
                if value is MISSING:
                    remaining.pop(key, None)
                else:
                    remaining[key] = value
            return remaining if remaining else MISSING
        if collection_depth and isinstance(before, list) and isinstance(current, list):
            remaining = _list_delta(current, before, [])
            return remaining if remaining else MISSING
        return MISSING
    if isinstance(before, list) and isinstance(after, list):
        return _list_delta(current if isinstance(current, list) else [], before, after)
    if isinstance(after, dict) and (isinstance(before, dict) or before is MISSING):
        if current is MISSING and not collection_depth:
            # An explicitly edited record may have been deleted by a peer.
            # Rebuild only that record, not the request's unchanged siblings.
            return copy.deepcopy(after)
        if current is not MISSING and not isinstance(current, dict):
            return copy.deepcopy(after)
        result = copy.deepcopy(current) if isinstance(current, dict) else {}
        prior = before if isinstance(before, dict) else {}
        for key in prior.keys() | after.keys():
            value = _delta(result.get(key, MISSING), prior.get(key, MISSING), after.get(key, MISSING),
                           collection_depth=max(0, collection_depth - 1))
            if value is MISSING:
                result.pop(key, None)
            else:
                result[key] = value
        return result
    if before is MISSING and isinstance(after, list) and isinstance(current, list):
        return _list_delta(current, [], after)
    return copy.deepcopy(after)


def _apply(state: dict, before: dict, after: dict, keys) -> None:
    for key in keys:
        value = _delta(state.get(key, MISSING), before.get(key, MISSING), after.get(key, MISSING),
                       collection_depth=2 if key == PROJECT_MAPPING else 1)
        if value is MISSING:
            state.pop(key, None)
        else:
            state[key] = value


def _bootstrap_order(native: list, managed: list, path: tuple[str, ...], conflicts: list[str]) -> list:
    """Combine unambiguous ID additions while preserving each side's order."""
    if (not all(isinstance(identifier, str) for identifier in (*native, *managed))
            or len(set(native)) != len(native) or len(set(managed)) != len(managed)):
        conflicts.append(".".join(path))
        return copy.deepcopy(native)
    native_ids, managed_ids = set(native), set(managed)
    native_positions = {identifier: index for index, identifier in enumerate(native)}
    managed_positions = {identifier: index for index, identifier in enumerate(managed)}
    anchors = [identifier for identifier in native if identifier in managed_ids]
    if anchors != [identifier for identifier in managed if identifier in native_ids]:
        conflicts.append(".".join(path))
        return copy.deepcopy(native)
    result = []
    native_at = managed_at = 0
    for anchor in [*anchors, None]:
        native_end = native_positions[anchor] if anchor is not None else len(native)
        managed_end = managed_positions[anchor] if anchor is not None else len(managed)
        native_extra = native[native_at:native_end]
        managed_extra = managed[managed_at:managed_end]
        if native_extra and managed_extra:
            conflicts.append(".".join(path))
            return copy.deepcopy(native)
        result.extend(native_extra or managed_extra)
        if anchor is not None:
            result.append(anchor)
        native_at, managed_at = native_end + 1, managed_end + 1
    return result


def _bootstrap_merge(native, managed, path: tuple[str, ...], conflicts: list[str]):
    """Admit additions; refuse same-field conflicts without an import baseline."""
    if native == managed:
        return copy.deepcopy(native)
    if isinstance(native, dict) and isinstance(managed, dict):
        result = copy.deepcopy(native)
        for key, value in managed.items():
            if key not in result:
                result[key] = copy.deepcopy(value)
            else:
                result[key] = _bootstrap_merge(result[key], value, (*path, key), conflicts)
        return result
    if isinstance(native, list) and isinstance(managed, list) and (
            (len(path) == 1 and path[0] in LIST_KEYS)
            or (len(path) >= 2 and path[0] in ORDER_MAP_KEYS)):
        return _bootstrap_order(native, managed, path, conflicts)
    conflicts.append(".".join(path))
    return copy.deepcopy(native)


def _assigned_project_threads(state: dict) -> set[str]:
    assignments = state.get("thread-project-assignments", {})
    return {identifier for identifier, value in assignments.items()
            if isinstance(value, dict) and isinstance(value.get("projectId"), str)
            and value["projectId"]}


def _bootstrap_state(source: Path, homes: list[Path]) -> tuple[dict, list[str]]:
    state = _canonical_state(_read_object(source / GLOBAL_STATE, missing=True), source, source)
    official_assigned = _assigned_project_threads(state)
    conflicts = []
    for home in homes:
        managed = _canonical_state(_read_object(home / GLOBAL_STATE, missing=True), home, source)
        if "projectless-thread-ids" in managed:
            # The old snapshot may still list a thread that the official
            # window has since assigned to a project. It is no longer a
            # projectless addition and must not create an order conflict.
            managed["projectless-thread-ids"] = [identifier for identifier in managed["projectless-thread-ids"]
                                                 if identifier not in official_assigned]
        state = _bootstrap_merge(state, managed, (), conflicts)
    return state, sorted(set(conflicts))


def bootstrap_preview(root: Path, source_home: Path, profile_home: Path) -> dict:
    """Read-only first-activation conflict check before copying the desktop app."""
    root, source, _, homes = _context(root, source_home, profile_home)
    if (root / "shared/sidebar-state.json").exists():
        return {"conflicts": [], "existing_state": True}
    _, conflicts = _bootstrap_state(source, homes)
    return {"conflicts": conflicts, "existing_state": False}


def _request_changes(request: dict, profile: Path, source: Path) -> tuple[dict, dict, tuple]:
    if not isinstance(request, dict) or request.get("action") not in {"snapshot", "commit"}:
        raise SidebarSyncError("未知侧栏同步请求。")
    if request["action"] == "snapshot":
        return {}, {}, ()
    changes = request.get("changes")
    if not isinstance(changes, dict) or any(key not in KEYS for key in changes):
        raise SidebarSyncError("侧栏提交包含非共享字段或无效 changes。")
    before, after = {}, {}
    for key, change in changes.items():
        if not isinstance(change, dict):
            raise SidebarSyncError(f"侧栏提交字段格式错误：{key}")
        for label, destination in (("before", before), ("after", after)):
            present = change.get(label + "_present")
            if not isinstance(present, bool) or (present and label not in change):
                raise SidebarSyncError(f"侧栏提交缺少 {label} 值或存在标记：{key}")
            if present:
                destination[key] = _canonical_value(key, change[label], profile, source)
    return before, after, tuple(changes)


def _atomic_write(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".sidebar-state-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def process_request(root: Path, source_home: Path, profile_home: Path, request: dict) -> dict:
    """Initialize or update the owned canonical sidebar and return its projection.

    Call ``snapshot`` once during activation after preparing all profile homes.
    Same-field initialization conflicts fail without a reliable import baseline.
    This bootstrap happens once, so old saved homes cannot later restore deleted
    records. A request never writes either official or profile state.
    """
    root, source, profile, homes = _context(root, source_home, profile_home)
    before, after, changed_request_keys = _request_changes(request, profile, source)
    shared = root / "shared"
    shared.mkdir(mode=0o700, exist_ok=True)
    state_path = shared / "sidebar-state.json"
    lock_path = shared / "sidebar-state.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        native = _canonical_state(_read_object(source / GLOBAL_STATE, missing=True), source, source)
        existing = state_path.exists()
        if existing:
            document = _read_object(state_path)
            if (document.get("version") != 1 or document.get("source_home") != str(source)
                    or not isinstance(document.get("revision"), int) or isinstance(document["revision"], bool)
                    or document["revision"] < 1
                    or not isinstance(document.get("state"), dict)
                    or not isinstance(document.get("native_baseline"), dict)):
                raise SidebarSyncError("共享侧栏状态标识或结构错误。")
            state = document["state"]
            baseline = document["native_baseline"]
            if baseline and not (source / GLOBAL_STATE).exists():
                raise SidebarSyncError("原 Codex 侧栏文件暂时不存在，保留共享状态。")
            if any(key not in KEYS for key in state.keys() | baseline.keys()):
                raise SidebarSyncError("共享侧栏状态含非共享字段。")
            for values in (state, baseline):
                for key, value in values.items():
                    _validate_value(key, value)
            previous = copy.deepcopy(state)
            _apply(state, baseline, native, KEYS)
        else:
            state, conflicts = _bootstrap_state(source, homes)
            if conflicts:
                preview = "、".join(conflicts[:12])
                if len(conflicts) > 12:
                    preview += f" 等共 {len(conflicts)} 项"
                raise SidebarSyncError("首次侧栏同步无法判定两侧字段的新旧值：" + preview +
                                       "。未写入共享侧栏，请核对原生与工具副本。", conflicts)
            # Old imported snapshots can still list a thread as projectless
            # after the official window assigned it to a project. Reconcile
            # this known cross-key conflict once, when snapshots are combined.
            if "projectless-thread-ids" in state:
                assigned = _assigned_project_threads(state)
                state["projectless-thread-ids"] = [identifier for identifier in state["projectless-thread-ids"]
                                                   if identifier not in assigned]
            previous = {}
            document = {"version": 1, "source_home": str(source), "revision": 1}
            baseline = {}
        _apply(state, before, after, changed_request_keys)
        changed_keys = [key for key in KEYS if not _equal(previous.get(key, MISSING), state.get(key, MISSING))]
        if existing and changed_keys:
            document["revision"] += 1
        document.update(state=state, native_baseline=native)
        if not existing or changed_keys or baseline != native:
            _atomic_write(state_path, document)
        return {"state": _project(state, source, profile), "changed_keys": changed_keys,
                "revision": document["revision"], "keys": list(KEYS)}


def snapshot(root: Path, source_home: Path, profile_home: Path) -> dict:
    return process_request(root, source_home, profile_home, {"action": "snapshot"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source-home", required=True, type=Path)
    parser.add_argument("--profile-home", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        request = json.load(sys.stdin)
        result = process_request(arguments.root, arguments.source_home, arguments.profile_home, request)
    except (SidebarSyncError, OSError, json.JSONDecodeError) as error:
        report = {"error": str(error)}
        if isinstance(error, SidebarSyncError) and error.conflicts:
            report["conflicts"] = error.conflicts
        print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
