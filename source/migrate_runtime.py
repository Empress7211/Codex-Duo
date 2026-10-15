"""Move one stopped Orbit runtime out of macOS's protected Documents folder.

The history in ~/.codex is never moved. The old and new runtime must be on the
same filesystem so the directory switch is a rename rather than a copy.
"""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path

from backend import Manager
from managed_desktop import verify_copy, profile_app_path
import json
from shared_store import canonicalize_rollout_paths


def _text_paths(root: Path) -> list[Path]:
    files = [root / "manifest.json", root / "desktop/copy.json"]
    files.extend(root.glob(".history-links-*.json"))
    for profile in (root / "profiles").iterdir():
        home = profile / "codex-home"
        files.extend([home / "config.toml", home / ".codex-global-state.json",
                      profile / "user-data/sentry/scope_v3.json", profile / "desktop/copy.json"])
    return [path for path in files if path.is_file() and not path.is_symlink()]


def _rewrite_file(path: Path, before: str, after: str) -> bool:
    contents = path.read_text()
    if before not in contents:
        return False
    replacement = path.with_name(path.name + ".orbit-migrating")
    replacement.write_text(contents.replace(before, after))
    replacement.chmod(path.stat().st_mode & 0o777)
    os.replace(replacement, path)
    return True


def _rewrite_links(root: Path, before: str, after: str) -> int:
    changed = 0
    for directory, subdirs, files in os.walk(root, followlinks=False):
        for name in [*subdirs, *files]:
            path = Path(directory) / name
            if not path.is_symlink():
                continue
            target = os.readlink(path)
            if target.startswith(before + "/"):
                replacement = path.with_name(path.name + ".orbit-migrating")
                replacement.symlink_to(after + target[len(before):])
                os.replace(replacement, path)
                changed += 1
    return changed


def move_runtime(old: Path, new: Path) -> dict:
    old = old.expanduser().resolve(strict=True)
    new = new.expanduser().absolute()
    if new.exists() or new.is_symlink():
        raise RuntimeError("Destination already exists; refusing to overwrite it")
    new.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if old.stat().st_dev != new.parent.stat().st_dev:
        raise RuntimeError("Runtime move must remain on one filesystem")
    old_lock = old.parent / ("." + old.name + ".control.lock")
    new_lock = new.parent / ("." + new.name + ".control.lock")
    with old_lock.open("a") as first, new_lock.open("a") as second:
        for stream in (first, second):
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manager = Manager(old)
        manifest = manager.owned()
        manager.require_stopped()
        _verify_desktops(manager)
        # Shared rollouts stay in ~/.codex, but their database references may
        # still use this runtime's profile symlinks. Remove that dependency
        # before rename makes those paths disappear. Canonical paths remain
        # valid even if the subsequent runtime switch has to roll back.
        history_updates = []
        if manifest.get('sync', {}).get('enabled'):
            history_updates = canonicalize_rollout_paths(manager.history_sqlite(), {
                manager.home(item['id']) / name: (manager.home(item['id']) / name).resolve()
                for item in manager.profiles() for name in ('sessions', 'archived_sessions')},
                lock_dir=Path(manifest['sync']['thread-writer-locks']))
        before, after = str(old), str(new)
        old.rename(new)
        try:
            files = sum(_rewrite_file(path, before, after) for path in _text_paths(new))
            links = _rewrite_links(new, before, after)
            moved = Manager(new)
            moved.owned()
            moved.require_stopped()
            _verify_desktops(moved)
            return {"old": before, "new": after, "updated_files": files,
                    "updated_links": links, "profiles": len(moved.profiles()),
                    "canonicalized_rollouts": len(history_updates)}
        except BaseException:
            for path in _text_paths(new):
                _rewrite_file(path, after, before)
            _rewrite_links(new, after, before)
            new.rename(old)
            raise


def _verify_desktops(manager: Manager) -> None:
    template = manager.desktop_app()
    if template.exists():
        verify_copy(template, allow_legacy_display=True)
    for profile in manager.profiles():
        app = profile_app_path(manager.root, profile)
        if app.exists():
            record = json.loads((app.parent / 'copy.json').read_text())
            verify_copy(app, profile_id=profile['id'], display_name=record['display_name'])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    args = parser.parse_args()
    print(move_runtime(args.old, args.new))
