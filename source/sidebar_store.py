"""Import the local Codex sidebar without importing account or runtime state.

Call only while the destination Codex instance is stopped: desktop global state
is cached in memory and can overwrite external changes. Source is read-only.
This handles the installed desktop schema, not cloud ChatGPT sidebar caches.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile


GLOBAL_STATE = ".codex-global-state.json"
ATOMS = "electron-persisted-atom-state"
PROJECT_MAPPING = "app-server-project-id-by-legacy-project-id-by-host"
MIGRATION_CHECKPOINTS = "app-server-projects-migration-by-host"
TOP_LEVEL_KEYS = frozenset({
    "local-projects", "project-order", "project-appearances",
    "thread-project-assignments", "projectless-thread-ids",
    "thread-project-membership-host-ids", "sidebar-project-thread-orders",
    "sidebar-thread-metadata", "thread-workspace-root-hints",
    "thread-projectless-output-directories", "pinned-thread-ids", "pinned-project-ids",
})
ATOM_KEYS = frozenset({
    "flat-project-sidebar-preferences-v1", "unified-sidebar-chat-order-v1",
    "unified-sidebar-project-order-v1", "unified-sidebar-pinned-order-v1",
    "codex-sidebar-sort-mode-v1", "pinned-sidebar-sort-mode-v1",
    "sidebar-collapsed-sections-v1", "sidebar-project-list-expanded-v1",
})
ATOM_PREFIX = "sidebar-project-expanded-v1-codex:"


class SidebarError(ValueError):
    pass


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise SidebarError("Codex 全局状态 JSON 格式错误，未写入。") from None
    if not isinstance(value, dict):
        raise SidebarError("Codex 全局状态必须是 JSON 对象，未写入。")
    return value


def _merge(preferred, other):
    """Keep destination-only records while using the chosen conflict priority."""
    if isinstance(preferred, dict) and isinstance(other, dict):
        result = copy.deepcopy(other)
        for key, value in preferred.items():
            result[key] = _merge(value, other[key]) if key in other else copy.deepcopy(value)
        return result
    if isinstance(preferred, list) and isinstance(other, list):
        result = copy.deepcopy(preferred)
        # Sidebar order IDs are hashable strings. Preserve ordered uniqueness
        # without quadratic membership tests on large local thread histories.
        seen = {json.dumps(value, sort_keys=True) for value in result}
        for value in other:
            identity = json.dumps(value, sort_keys=True)
            if identity not in seen:
                result.append(copy.deepcopy(value))
                seen.add(identity)
        return result
    return copy.deepcopy(preferred)


def _atomic_write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".sidebar-import-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def seed_sidebar(source_home: Path, target_home: Path, mode: str = "seed") -> dict:
    """Seed or repair one stopped managed home; return non-secret counts.

    ``seed`` gives existing destination values priority on conflicts; ``repair``
    restores source sidebar values on conflicts. Both retain destination-only
    projects, threads, list entries, and all non-whitelisted runtime/account
    fields. The caller verifies ownership and that managed processes are stopped.

    The legacy→server mapping is imported only for the source local host and
    rekeyed to the destination local host. That destination's migration checkpoint
    is removed so imported assignments can be migrated on its next launch.
    """
    if mode not in {"seed", "repair"}:
        raise SidebarError("未知侧栏导入模式。")
    source_home = Path(source_home).expanduser().resolve()
    target_home = Path(target_home).expanduser().resolve()
    if target_home == source_home or target_home.is_relative_to(source_home):
        raise SidebarError("目标目录不得位于原 Codex 目录内。")
    source_path = source_home / GLOBAL_STATE
    target_path = target_home / GLOBAL_STATE
    if target_path.is_symlink():
        raise SidebarError("拒绝写入指向其他文件的全局状态符号链接。")
    if source_path.exists() and target_path.exists() and os.path.samefile(source_path, target_path):
        raise SidebarError("目标全局状态与原文件相同，已停止。")
    if not source_path.exists():
        return {"changed": False, "source_missing": True, "top_level_keys": 0,
                "atom_keys": 0, "projects": 0, "thread_assignments": 0,
                "projectless_threads": 0, "project_mappings": 0,
                "nonidentity_project_mappings": 0, "checkpoints_cleared": 0}

    source = _read(source_path)
    target = _read(target_path)
    updated = copy.deepcopy(target)

    def merge_key(destination, key, value):
        if key not in destination:
            destination[key] = copy.deepcopy(value)
        elif mode == "repair":
            destination[key] = _merge(value, destination[key])
        else:
            destination[key] = _merge(destination[key], value)

    imported = sorted(TOP_LEVEL_KEYS.intersection(source))
    for key in imported:
        merge_key(updated, key, source[key])

    source_atoms = source.get(ATOMS, {})
    target_atoms = updated.get(ATOMS, {})
    if not isinstance(source_atoms, dict) or not isinstance(target_atoms, dict):
        raise SidebarError("Codex 侧栏偏好结构错误，未写入。")
    atom_keys = sorted(key for key in source_atoms if key in ATOM_KEYS or key.startswith(ATOM_PREFIX))
    for key in atom_keys:
        merge_key(target_atoms, key, source_atoms[key])
    if atom_keys:
        updated[ATOMS] = target_atoms

    source_host = "local:" + str(source_home)
    target_host = "local:" + str(target_home)
    source_mappings = source.get(PROJECT_MAPPING, {})
    target_mappings = updated.get(PROJECT_MAPPING, {})
    checkpoints = updated.get(MIGRATION_CHECKPOINTS, {})
    if not all(isinstance(value, dict) for value in (source_mappings, target_mappings, checkpoints)):
        raise SidebarError("Codex 项目迁移结构错误，未写入。")
    local_mapping = source_mappings.get(source_host, {})
    if not isinstance(local_mapping, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in local_mapping.items()):
        raise SidebarError("Codex 项目标识映射结构错误，未写入。")
    if local_mapping:
        merge_key(target_mappings, target_host, local_mapping)
        updated[PROJECT_MAPPING] = target_mappings
    checkpoints_cleared = int(target_host in checkpoints)
    if checkpoints_cleared:
        del checkpoints[target_host]
        updated[MIGRATION_CHECKPOINTS] = checkpoints

    changed = updated != target
    if changed:
        _atomic_write(target_path, updated)
    return {
        "changed": changed, "source_missing": False,
        "top_level_keys": len(imported), "atom_keys": len(atom_keys),
        "projects": len(source.get("local-projects", {})),
        "thread_assignments": len(source.get("thread-project-assignments", {})),
        "projectless_threads": len(source.get("projectless-thread-ids", [])),
        "project_mappings": len(local_mapping),
        "nonidentity_project_mappings": sum(key != value for key, value in local_mapping.items()),
        "checkpoints_cleared": checkpoints_cleared,
    }
