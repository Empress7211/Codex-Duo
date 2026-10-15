"""Independent shared history for the two managed Codex instances.

The caller must stop both managed instances before import. Source databases are
opened read-only; source processes may continue, so import is a point-in-time
copy, not live synchronization. Never point these links at the original home.
"""
from __future__ import annotations

from contextlib import contextmanager, closing

import json
import ctypes
import errno
import sys
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

SHARED_DIRS = ("sessions", "archived_sessions", "thread-writer-locks")
DATABASES = ("state_5.sqlite", "thread_history_1.sqlite")
_STATE_KEEP = {
    "_sqlx_migrations", "sqlite_sequence", "threads", "thread_dynamic_tools",
    "thread_spawn_edges", "thread_sections", "projects", "project_roots",
}
_HISTORY_KEEP = {
    "_sqlx_migrations", "sqlite_sequence", "thread_turns", "thread_items",
    "thread_history_projection_state", "thread_realtime_items",
}


@contextmanager
def _readonly(path: Path):
    # sqlite.Connection's context manager commits, but does not close handles.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        yield db


def initialize_shared(root: Path) -> None:
    root = Path(root).resolve()
    _recover_publication(root)
    for name in ("sqlite", *SHARED_DIRS):
        (root / "shared" / name).mkdir(parents=True, exist_ok=True, mode=0o700)


def attach_home(root: Path, home: Path) -> None:
    """Attach only absent paths or already-correct links in a managed home."""
    root, home = Path(root).resolve(), Path(home)
    initialize_shared(root)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in SHARED_DIRS:
        target, link = root / "shared" / name, home / name
        if link.is_symlink() and link.resolve() == target:
            continue
        if link.exists() or link.is_symlink():
            raise FileExistsError(f"Refusing to replace existing history path: {link}")
        link.symlink_to(target, target_is_directory=True)


def count_threads(sqlite_dir: Path) -> int:
    path = Path(sqlite_dir) / DATABASES[0]
    if not path.exists():
        return 0
    with _readonly(path) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'").fetchone()
        return db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] if exists else 0


def _backup(source: Path, target: Path) -> None:
    with _readonly(source) as src, closing(sqlite3.connect(target)) as dst, dst:
        # sqlite.backup understands WAL and bounds memory; copying the database
        # file alone would miss committed data while the source app is running.
        # Pin one WAL read snapshot so an active source cannot repeatedly
        # restart this multi-GB backup as new source commits arrive.
        src.execute("BEGIN")
        src.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        deadline = time.monotonic() + 300

        def check_deadline(status: int, remaining: int, total: int) -> None:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"History database snapshot exceeded 300 seconds: {source.name}; "
                    f"{remaining} of {total} pages remaining (SQLite status {status})"
                )

        src.backup(dst, pages=2048, progress=check_deadline, sleep=0.05)
        src.rollback()
        dst.execute("PRAGMA journal_mode=DELETE")


def _clear_nonhistory(db: sqlite3.Connection, keep: set[str]) -> dict[str, int]:
    removed = {}
    # Keep schema/migrations intact so the current CLI can reopen the snapshot.
    db.execute("PRAGMA secure_delete=ON")
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        if table in keep or table.startswith("sqlite_"):
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        n = db.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        if n:
            db.execute(f"DELETE FROM {quoted}")
            removed[table] = n
    return removed


def _clone_file(source: Path, target: Path) -> bool:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        clone = getattr(libc, "clonefile", None)
        if clone is not None:
            clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            clone.restype = ctypes.c_int
            if clone(os.fsencode(source), os.fsencode(target), 0) == 0:
                return True
            else:
                error = ctypes.get_errno()
                if error not in {errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}:
                    raise OSError(error, os.strerror(error), str(source))
    return False


def _copy_rollout(source: Path, target: Path) -> tuple[int, int]:
    """Copy one append-only rollout prefix; exclude an unfinished final line."""
    before = source.stat()
    target.parent.mkdir(parents=True, exist_ok=True)
    cloned = _clone_file(source, target)
    if not cloned:
        with source.open("rb") as src, target.open("xb") as dst:
            remaining = before.st_size
            while remaining:
                chunk = src.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError(f"Source rollout shrank during import: {source}")
                dst.write(chunk)
                remaining -= len(chunk)
    after = source.stat()
    if before.st_ino != after.st_ino or after.st_size < before.st_size:
        raise RuntimeError(f"Source rollout was replaced during import: {source}")
    size = target.stat().st_size
    # A running source can end the snapshot in the middle of one JSONL record.
    with target.open("r+b") as dst:
        if size:
            dst.seek(size - 1)
            if dst.read(1) != b"\n":
                end = size
                while end:
                    start = max(0, end - 65536)
                    dst.seek(start)
                    data = dst.read(end - start)
                    pos = data.rfind(b"\n")
                    if pos >= 0:
                        size = start + pos + 1
                        break
                    end = start
                else:
                    size = 0
                dst.truncate(size)
    if not size:
        raise RuntimeError(f"No complete records in source rollout: {source}")
    return size, int(after.st_size != before.st_size)



def _member_span(text: str, object_start: int, key: str) -> tuple[int, int] | None:
    decoder = json.JSONDecoder()
    cursor = object_start + 1
    while True:
        while text[cursor].isspace() or text[cursor] == ",":
            cursor += 1
        if text[cursor] == "}":
            return None
        name, cursor = decoder.raw_decode(text, cursor)
        while text[cursor].isspace():
            cursor += 1
        if text[cursor] != ":":
            raise ValueError("Invalid JSON object separator")
        cursor += 1
        while text[cursor].isspace():
            cursor += 1
        start = cursor
        _, cursor = decoder.raw_decode(text, cursor)
        if name == key:
            return start, cursor


def _normalized_provider_header(first_line: bytes) -> tuple[bytes | None, str | None]:
    """Change only the top-level session metadata provider, preserving offsets."""
    text = first_line.decode("utf-8")
    data = json.loads(text)
    if data.get("type") != "session_meta":
        return None, "non_session_meta"
    payload_span = _member_span(text, len(text) - len(text.lstrip()), "payload")
    provider_span = _member_span(text, payload_span[0], "model_provider") if payload_span else None
    if provider_span is None or not isinstance(data.get("payload", {}).get("model_provider"), str):
        return None, "missing_provider"
    start, end = provider_span
    replacement = b'"custom"'
    original_bytes = text[start:end].encode("utf-8")
    if len(original_bytes) < len(replacement):
        return None, "short_provider"
    new = (text[:start].encode("utf-8") + replacement +
           b" " * (len(original_bytes) - len(replacement)) + text[end:].encode("utf-8"))
    assert len(new) == len(first_line)
    return new, None


def _replace_first_line(path: Path, first_line: bytes) -> None:
    with path.open("rb") as handle:
        old = handle.readline()
    if len(old) != len(first_line):
        raise RuntimeError(f"Cannot change paginated rollout byte offsets: {path.name}")
    temporary_dir = Path(tempfile.mkdtemp(prefix=".provider-normalize-", dir=path.parent))
    temporary = temporary_dir / "rollout"
    try:
        if not _clone_file(path, temporary):
            shutil.copyfile(path, temporary)
        with temporary.open("r+b") as handle:
            handle.write(first_line)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


def _normalize_provider_store(store: Path, recorded_root: Path) -> dict:
    report = {"normalized_to": "custom", "threads_checked": 0, "database_rows_changed": 0,
              "rollout_headers_changed": 0, "skipped_missing_provider": 0,
              "skipped_short_provider": 0, "skipped_non_session_meta": 0, "backup": None}
    if not (store / "sqlite" / DATABASES[0]).resolve().is_relative_to(store.resolve()):
        raise RuntimeError("Managed state database points outside the independent shared store")
    backup_dir = store / "provider-backups" / str(time.time_ns())
    journal = None
    try:
        with closing(sqlite3.connect(store / "sqlite" / DATABASES[0])) as db, db:
            for tid, recorded_path, provider in db.execute("SELECT id,rollout_path,model_provider FROM threads"):
                report["threads_checked"] += 1
                relative = Path(recorded_path).resolve().relative_to(recorded_root.resolve())
                path = store / relative
                if not path.resolve().is_relative_to(store.resolve()):
                    raise RuntimeError("Managed rollout points outside the independent shared store")
                with path.open("rb") as handle:
                    old = handle.readline()
                new, skipped = _normalized_provider_header(old)
                if skipped:
                    report["skipped_" + skipped] += 1
                    continue
                if new == old and provider == "custom":
                    continue
                if journal is None:
                    backup_dir.mkdir(parents=True, mode=0o700)
                    journal = (backup_dir / "originals.jsonl").open("x", encoding="utf-8")
                    report["backup"] = str(backup_dir.relative_to(store))
                # Persist undo information before touching either managed copy.
                journal.write(json.dumps({"thread_id": tid, "rollout": str(relative),
                                          "provider": provider, "first_line": old.decode("utf-8") if new != old else None},
                                         ensure_ascii=False) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
                if new != old:
                    _replace_first_line(path, new)
                    report["rollout_headers_changed"] += 1
                if provider != "custom":
                    db.execute("UPDATE threads SET model_provider='custom' WHERE id=?", (tid,))
                    report["database_rows_changed"] += 1
        if journal:
            (backup_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    finally:
        if journal:
            journal.close()


def normalize_managed_provider(root: Path) -> dict:
    """Normalize independent copies only; caller must stop both managed apps."""
    shared = Path(root).resolve() / "shared"
    if shared.is_symlink():
        raise RuntimeError("Managed shared store must not be a symbolic link")
    return _normalize_provider_store(shared, shared)


def restore_managed_provider(root: Path, backup: str) -> dict:
    """Restore one normalization's original headers/providers, with apps stopped."""
    shared = Path(root).resolve() / "shared"
    backup_dir = (shared / backup).resolve()
    if not backup_dir.is_relative_to((shared / "provider-backups").resolve()):
        raise ValueError("Provider backup is outside the managed backup directory")
    restored = 0
    with (backup_dir / "originals.jsonl").open(encoding="utf-8") as journal, closing(sqlite3.connect(shared / "sqlite" / DATABASES[0])) as db, db:
        for line in journal:
            entry = json.loads(line)
            path = (shared / entry["rollout"]).resolve()
            if not path.is_relative_to(shared):
                raise ValueError("Provider backup rollout is outside the managed shared store")
            if entry["first_line"] is not None:
                _replace_first_line(path, entry["first_line"].encode("utf-8"))
            db.execute("UPDATE threads SET model_provider=? WHERE id=?", (entry["provider"], entry["thread_id"]))
            restored += 1
    return {"restored_threads": restored}


def _journal_staging(root: Path, state: dict) -> Path:
    staging = Path(state["staging"])
    if staging.parent != root or not staging.name.startswith(".history-import-"):
        raise RuntimeError("Invalid history import recovery journal")
    return staging


def _write_journal(root: Path, state: dict) -> None:
    marker = root / ".history-import-publish.json"
    temporary = marker.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, marker)


def _recover_publication(root: Path) -> None:
    marker = root / ".history-import-publish.json"
    if not marker.exists():
        return
    state = json.loads(marker.read_text(encoding="utf-8"))
    if state["status"] == "ready":
        return
    staging = _journal_staging(root, state)
    for entry in reversed(state["entries"]):
        relative = Path(entry["path"])
        staged, target = staging / relative, root / "shared" / relative
        previous = staging / "previous" / relative
        # A missing replacement means its rename already happened. Move it
        # back before restoring the pre-import empty store, even after a crash.
        if entry["replacement"] and not staged.exists() and target.exists():
            staged.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, staged)
        if previous.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(previous, target)
    state["status"] = "ready"
    _write_journal(root, state)


def _publish_staging(root: Path, staging: Path) -> None:
    # Only these history paths are replaced; shared skills and other resources
    # remain exactly where they are. The journal precedes the first rename.
    paths = [*SHARED_DIRS, "history-import.json", "provider-backups"]
    paths += ["sqlite/" + name + suffix for name in DATABASES for suffix in ("", "-wal", "-shm")]
    entries = [{"path": path, "replacement": (staging / path).exists()} for path in paths]
    state = {"staging": str(staging), "status": "publishing", "entries": entries}
    _write_journal(root, state)
    try:
        for entry in entries:
            relative = Path(entry["path"])
            target, previous = root / "shared" / relative, staging / "previous" / relative
            previous.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                os.replace(target, previous)
            if entry["replacement"]:
                os.replace(staging / relative, target)
        (root / ".history-import-publish.json").unlink()
    except BaseException:
        _recover_publication(root)
        raise

def import_history(source_home: Path, root: Path) -> dict:
    source_home, root = Path(source_home).resolve(), Path(root).resolve()
    initialize_shared(root)
    shared = root / "shared"
    if source_home == shared or shared in source_home.parents or source_home in shared.parents:
        raise ValueError("Source and managed shared store must be independent directories")
    if count_threads(shared / "sqlite"):
        raise RuntimeError("Shared store already contains threads; import is only allowed into an empty store")
    for name in SHARED_DIRS:
        if any((shared / name).iterdir()):
            raise RuntimeError(f"Shared {name} is not empty; refusing to overwrite it")
    marker = root / ".history-import-publish.json"
    if marker.exists():
        state = json.loads(marker.read_text(encoding="utf-8"))
        staging = _journal_staging(root, state)
        report = json.loads((staging / "history-import.json").read_text(encoding="utf-8"))
        if report["source_home"] != str(source_home):
            raise RuntimeError("A completed snapshot from another source is pending; finish that import first")
        _publish_staging(root, staging)
        shutil.rmtree(staging)
        return report
    source_db = source_home / DATABASES[0]
    if not source_db.is_file():
        raise FileNotFoundError(f"Current Codex state database not found: {source_db}")
    # Space estimate uses only SQLite metadata and rollout file sizes, never
    # reads/hashes the multi-GB history solely for validation.
    required = sum((source_home / name).stat().st_size for name in DATABASES if (source_home / name).exists())
    with _readonly(source_db) as db:
        for (path,) in db.execute("SELECT rollout_path FROM threads"):
            required += Path(path).stat().st_size
    free = shutil.disk_usage(root).free
    if free < required + max(128 * 1024 * 1024, required // 10):
        raise OSError(f"Not enough free space for history snapshot: need about {required} bytes plus margin, have {free}")
    staging = Path(tempfile.mkdtemp(prefix=".history-import-", dir=root))
    started = time.time()
    report = {"threads": 0, "rollout_bytes": 0, "source_files_grew": 0, "removed_rows": {}, "snapshot": True}
    try:
        for name in ("sqlite", *SHARED_DIRS):
            (staging / name).mkdir()
        for name in DATABASES:
            source = source_home / name
            if source.exists():
                _backup(source, staging / "sqlite" / name)
        with closing(sqlite3.connect(staging / "sqlite" / DATABASES[0])) as db, db:
            report["removed_rows"][DATABASES[0]] = _clear_nonhistory(db, _STATE_KEEP)
            # Iterate the small metadata cursor; SQLite history contents never
            # accumulate as Python objects. Preserve IDs and cwd; normalize only
            # the managed provider after all rollout copies are complete.
            for tid, old_path, archived in db.execute("SELECT id, rollout_path, archived FROM threads"):
                source = Path(old_path)
                directory = "archived_sessions" if archived else "sessions"
                try:
                    relative = source.resolve().relative_to((source_home / directory).resolve())
                except ValueError:
                    # An imported/older rollout may live outside the normal
                    # date hierarchy. Use its thread ID to avoid name clashes.
                    relative = Path("imported") / str(tid) / source.name
                relative_target = Path(directory) / relative
                size, grew = _copy_rollout(source, staging / relative_target)
                db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(shared / relative_target), tid))
                report["threads"] += 1
                report["rollout_bytes"] += size
                report["source_files_grew"] += grew
        history = staging / "sqlite" / DATABASES[1]
        if history.exists():
            with closing(sqlite3.connect(history)) as db, db:
                report["removed_rows"][DATABASES[1]] = _clear_nonhistory(db, _HISTORY_KEEP)
        report["provider_normalization"] = _normalize_provider_store(staging, shared)
        report["duration_seconds"] = round(time.time() - started, 2)
        report["source_home"] = str(source_home)
        report["note"] = "Independent snapshot; original app history will not stay in live sync. Source task execution, credentials, automations, queues, and worktree ownership were not imported."
        (staging / "history-import.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        _publish_staging(root, staging)
        return report
    finally:
        # A publication failure retains the complete snapshot and its recovery
        # journal. Retrying the import publishes that snapshot without recopying.
        if not (root / ".history-import-publish.json").exists():
            shutil.rmtree(staging, ignore_errors=True)
