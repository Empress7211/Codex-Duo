"""Switch stopped managed homes to one canonical history store.

The original application, credentials and configuration are never written here.
The previous managed ``shared`` directory stays in place for recovery. Callers
must stop each managed application before changing its links or configuration.
The preflight is a deliberate data-compatibility check for migration, not a
startup/status poll or proof of real-time UI synchronization.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import tomllib


HISTORY_DIRS = ("sessions", "archived_sessions", "thread-writer-locks")
STATE_DB = "state_5.sqlite"
HISTORY_DB = "thread_history_1.sqlite"


class SharedStoreError(RuntimeError):
    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report or {}


def canonical_paths(source_home: Path, sqlite_home: Path | None = None) -> dict:
    """Resolve source history paths without writing config or creating paths.

    ``sqlite_home`` can provide the actual native server's environment override.
    Otherwise the source config is used, with relative paths resolved beside it.
    """
    source = Path(source_home).expanduser().resolve()
    if sqlite_home is None:
        config = source / "config.toml"
        value = tomllib.loads(config.read_text()) if config.exists() else {}
        sqlite_home = value.get("sqlite_home", source)
    database_root = Path(sqlite_home).expanduser()
    if not database_root.is_absolute():
        database_root = source / database_root
    return {"source_home": source, "sqlite_home": database_root.resolve(),
            **{name: (source / name).resolve() for name in HISTORY_DIRS}}


def canonicalize_rollout_paths(sqlite_home: Path, aliases: dict[Path, Path],
                              *, lock_dir: Path | None = None) -> list[dict]:
    """Detach history references from profile directories before they disappear.

    Codex records the lexical CODEX_HOME path even when sessions is a symlink.
    Only caller-declared history aliases are changed, in one transaction; neither
    the rollout nor its projected messages and offsets are rewritten.
    """
    database = Path(sqlite_home) / STATE_DB
    if not database.is_file():
        return []  # An empty workspace need not have a history database yet.
    changes = []
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for alias, destination in aliases.items():
            prefix = str(alias) + "/"
            target_root = Path(destination).resolve()
            rows = db.execute("SELECT id,rollout_path FROM threads "
                              "WHERE substr(rollout_path,1,?)=?", (len(prefix), prefix))
            for tid, old in rows.fetchall():
                if lock_dir is not None:
                    from runtime_sync import lock_busy
                    if lock_busy(Path(lock_dir) / (tid + '.lock')):
                        raise SharedStoreError(f"会话仍由其他窗口占用，未切换目录：{tid}")
                relative = Path(old[len(prefix):])
                target = (target_root / relative).resolve()
                if (not target.is_relative_to(target_root)
                        or not target.name.endswith("-" + tid + ".jsonl")):
                    raise SharedStoreError(f"会话路径与共享文件标识不符，未切换目录：{tid}")
                compressed = Path(str(target) + ".zst")
                if not target.is_file() and not compressed.is_file():
                    raise SharedStoreError(f"共享会话文件不存在，未切换目录：{tid}")
                if target.is_file():
                    # Recovery from a vanished alias must identify the actual
                    # session, without reading its potentially large transcript.
                    with target.open("rb") as stream:
                        header = json.loads(stream.readline(1024 * 1024))
                    if (header.get("type") != "session_meta"
                            or header.get("payload", {}).get("id") != tid):
                        raise SharedStoreError(f"共享会话头部标识不符，未切换目录：{tid}")
                new = str(target)
                if old != new:
                    db.execute("UPDATE threads SET rollout_path=? WHERE id=? AND rollout_path=?",
                               (new, tid, old))
                    changes.append({"thread_id": tid, "before": old, "after": new})
    return changes


def _metadata(sqlite_home: Path, required: bool) -> tuple[dict, set]:
    path = sqlite_home / STATE_DB
    if not path.exists():
        if required:
            raise SharedStoreError(f"权威 Codex 历史库不存在：{path}")
        return {}, set()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        threads = {tid: {"rollout_path": path, "history_mode": mode}
                   for tid, path, mode in db.execute("SELECT id, rollout_path, history_mode FROM threads")}
        projects = {row[0] for row in db.execute("SELECT id FROM projects")}
    return threads, projects


def _history(sqlite_home: Path, required: bool) -> tuple[set, dict]:
    path = sqlite_home / HISTORY_DB
    if not path.exists():
        if required:
            raise SharedStoreError(f"权威 Codex 分页历史库不存在：{path}")
        return set(), {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        turns = set(db.execute("SELECT thread_id, turn_id FROM thread_turns"))
        projections = {tid: (offset, ordinal) for tid, offset, ordinal in db.execute(
            "SELECT thread_id, next_rollout_byte_offset, next_rollout_ordinal "
            "FROM thread_history_projection_state")}
    return turns, projections


def _settings_tail(path: Path, start: int, end: int, metrics: dict | None = None,
                   *, allow_nonsettings: bool = False) -> int | None:
    """Read only an additional suffix, stopping at the first actual work event."""
    events = 0
    with path.open("rb") as stream:
        stream.seek(start)
        remaining = end - start
        while remaining:
            # Settings records are small. Do not allocate a whole unexpected
            # multi-GB response line merely to reject a non-settings suffix.
            line = stream.readline(min(remaining, 1024 * 1024) + 1)
            if metrics is not None:
                metrics["bytes_read"] = metrics.get("bytes_read", 0) + len(line)
            if not line or len(line) > remaining or not line.endswith(b"\n"):
                raise SharedStoreError(f"无法确认完整设置事件尾部，保留原历史：{path.name}")
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                raise SharedStoreError(f"副本尾部不是完整 JSONL，保留原历史：{path.name}") from None
            if (not isinstance(event, dict) or event.get("type") != "event_msg"
                    or not isinstance(event.get("payload"), dict)
                    or event["payload"].get("type") != "thread_settings_applied"):
                if allow_nonsettings:
                    return None
                raise SharedStoreError(f"副本存在额外会话内容，不能零导入切换：{path.name}")
            remaining -= len(line)
            events += 1
    return events


def _item_versions(sqlite_home: Path, tid: str) -> tuple[dict, dict]:
    """Read one affected thread's version metadata, never its item_json blobs."""
    with closing(sqlite3.connect((sqlite_home / HISTORY_DB).as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        items = {(turn, item): (ordinal, updated, kind, created)
                 for turn, item, ordinal, updated, kind, created in db.execute(
            "SELECT turn_id,item_id,rollout_ordinal,updated_at_ordinal,item_type,created_at_ms "
            "FROM thread_items WHERE thread_id=?", (tid,))}
        realtime = {item: (ordinal, kind, created) for item, ordinal, kind, created in db.execute(
            "SELECT item_id,rollout_ordinal,item_type,created_at_ms "
            "FROM thread_realtime_items WHERE thread_id=?", (tid,))}
    return items, realtime


def _check_versions(native_sqlite: Path, managed_sqlite: Path, tid: str) -> dict:
    native_items, native_realtime = _item_versions(native_sqlite, tid)
    managed_items, managed_realtime = _item_versions(managed_sqlite, tid)
    for key, version in managed_items.items():
        native = native_items.get(key)
        if (native is None or native[0] != version[0] or native[1] < version[1]
                or native[2:] != version[2:]):
            raise SharedStoreError(f"分页副本有独有或更新的 item，不能零导入切换：{tid}")
    for key, version in managed_realtime.items():
        native = native_realtime.get(key)
        if native is None or native != version:
            raise SharedStoreError(f"分页副本有不同的 realtime item，不能零导入切换：{tid}")
    return {"items_checked": len(managed_items), "realtime_items_checked": len(managed_realtime)}


def _check_all_item_versions(native_sqlite: Path, managed_sqlite: Path) -> None:
    """Reject managed-only/newer indexed items without reading their JSON blobs.

    Ordinary rollout projection offsets cannot reveal changes stored only in
    ``thread_items`` or ``thread_realtime_items``. One read-only indexed join
    covers those rows for the one-time switch; exceptional paginated rollouts
    still receive the stronger physical-prefix check below.
    """
    managed_database = managed_sqlite / HISTORY_DB
    if not managed_database.exists():
        return  # A newly initialized workspace has no copied history to lose.
    managed_uri = managed_database.as_uri() + "?mode=ro"
    native_uri = (native_sqlite / HISTORY_DB).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(managed_uri, uri=True)) as db:
        db.execute("ATTACH DATABASE ? AS native", (native_uri,))
        db.execute("BEGIN")
        for table, difference in (
            ("thread_items", """
                n.item_id IS NULL OR n.rollout_ordinal IS NOT m.rollout_ordinal
                OR n.updated_at_ordinal < m.updated_at_ordinal
                OR n.item_type IS NOT m.item_type OR n.created_at_ms IS NOT m.created_at_ms
            """),
            ("thread_realtime_items", """
                n.item_id IS NULL OR n.rollout_ordinal IS NOT m.rollout_ordinal
                OR n.item_type IS NOT m.item_type OR n.created_at_ms IS NOT m.created_at_ms
            """),
        ):
            managed_exists = db.execute(
                "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not managed_exists:
                continue
            native_exists = db.execute(
                "SELECT 1 FROM native.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not native_exists:
                if db.execute(f"SELECT 1 FROM main.{table} LIMIT 1").fetchone():
                    raise SharedStoreError(f"副本存在权威库缺少的 {table} 内容，不能零导入切换。")
                continue
            key = ("m.thread_id=n.thread_id AND m.turn_id=n.turn_id AND m.item_id=n.item_id"
                   if table == "thread_items" else
                   "m.thread_id=n.thread_id AND m.item_id=n.item_id")
            row = db.execute(
                f"SELECT m.thread_id,m.item_id FROM main.{table} AS m "
                f"LEFT JOIN native.{table} AS n ON {key} WHERE {difference} LIMIT 1"
            ).fetchone()
            if row:
                label = "item" if table == "thread_items" else "realtime item"
                raise SharedStoreError(f"副本有独有或更新的 {label}，不能零导入切换：{row[0]}")


@contextmanager
def _native_rollout(path: Path):
    """Materialize one compressed native rollout only when a comparison needs it."""
    if path.exists():
        yield path
        return
    compressed = Path(str(path) + ".zst")
    if not compressed.is_file():
        raise SharedStoreError(f"权威历史文件不存在：{path.name}")
    before = compressed.stat()
    with tempfile.TemporaryDirectory(prefix="orbit-rollout-") as directory:
        target = Path(directory) / path.name
        try:
            from compression import zstd
        except ImportError:
            executable = shutil.which("zstd")
            if executable is None:
                raise SharedStoreError("权威历史已压缩；需要 Python zstd 支持或 zstd 命令进行只读预检。")
            with target.open("wb") as output:
                result = subprocess.run([executable, "-d", "-c", "--", str(compressed)],
                                        stdout=output, stderr=subprocess.DEVNULL)
            if result.returncode:
                raise SharedStoreError(f"权威压缩历史无法解码：{compressed.name}")
        else:
            try:
                with zstd.open(compressed, "rb") as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
            except (OSError, ValueError):
                raise SharedStoreError(f"权威压缩历史无法解码：{compressed.name}") from None
        after = compressed.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns):
            raise SharedStoreError(f"检查期间权威压缩历史发生变化：{compressed.name}")
        yield target


def _rollout_prefix(native_path: Path, managed_path: Path, tid: str,
                    native_projection: tuple, managed_projection: tuple,
                    require_base: bool, metrics: dict, *, check_tail: bool = True) -> dict:
    """Compare a changed rollout's common physical prefix and history_base.

    history_base keeps the durable database's old ordinal/byte boundary; those
    byte offsets cannot be used to seek the rewritten physical file. Comparing
    these exceptional files directly preserves unprojected managed work too.
    """
    native_before, managed_before = native_path.stat(), managed_path.stat()
    with native_path.open("rb") as native, managed_path.open("rb") as managed:
        native_line, managed_line = native.readline(), managed.readline()
        metrics["bytes_read"] = len(native_line) + len(managed_line)
        try:
            native_header, managed_header = json.loads(native_line), json.loads(managed_line)
        except (ValueError, UnicodeDecodeError):
            raise SharedStoreError(f"分页历史头部无法解析：{tid}") from None
        for header in (native_header, managed_header):
            if (not isinstance(header, dict) or header.get("type") != "session_meta"
                    or not isinstance(header.get("payload"), dict)):
                raise SharedStoreError(f"分页历史缺少 session_meta：{tid}")
            header["payload"].pop("model_provider", None)
        base = managed_header["payload"].get("history_base")
        if require_base and base is None:
            raise SharedStoreError(f"分页历史缺少明确 history_base，自动零导入切换尚未支持该模式：{tid}")
        if base is not None and (
                not isinstance(base, dict) or base.get("thread_id") != tid
                or not isinstance(base.get("end_ordinal_exclusive"), int)
                or not isinstance(base.get("end_byte_offset"), int)
                or managed_header["payload"].get("history_mode") != "paginated"):
            raise SharedStoreError(f"分页历史的 history_base 无法确认：{tid}")
        if native_header != managed_header:
            raise SharedStoreError(f"分页历史的基线或会话头部不同，保留两份历史：{tid}")
        if base is not None:
            base_offset, base_ordinal = base["end_byte_offset"], base["end_ordinal_exclusive"]
            for offset, ordinal in (native_projection, managed_projection):
                if min(base_offset, base_ordinal) < 0 or offset < base_offset or ordinal < base_ordinal:
                    raise SharedStoreError(f"分页 projection 早于声明的 history_base：{tid}")
        native_body = native_before.st_size - len(native_line)
        managed_body = managed_before.st_size - len(managed_line)
        compared = min(native_body, managed_body)
        remaining = compared
        common_line_end = 0
        compared_so_far = 0
        diverged = False
        while remaining:
            size = min(1024 * 1024, remaining)
            native_chunk, managed_chunk = native.read(size), managed.read(size)
            metrics["bytes_read"] += len(native_chunk) + len(managed_chunk)
            if len(native_chunk) != size or len(managed_chunk) != size:
                raise SharedStoreError(f"分页历史正文读取不完整：{tid}")
            if native_chunk != managed_chunk:
                first_difference = next(index for index, pair in enumerate(zip(native_chunk, managed_chunk))
                                        if pair[0] != pair[1])
                last_newline = native_chunk.rfind(b"\n", 0, first_difference)
                if last_newline >= 0:
                    common_line_end = compared_so_far + last_newline + 1
                diverged = True
                break
            last_newline = native_chunk.rfind(b"\n")
            if last_newline >= 0:
                common_line_end = compared_so_far + last_newline + 1
            compared_so_far += size
            remaining -= size
    events = 0
    tail_bytes = max(0, managed_body - native_body)
    divergent_suffixes = {}
    if diverged and check_tail:
        # Managed-only work cannot be dropped. Native may have continued with
        # real work while the stopped managed copy only changed settings.
        try:
            managed_events = _settings_tail(managed_path, len(managed_line) + common_line_end,
                                            managed_before.st_size, metrics)
            native_events = _settings_tail(native_path, len(native_line) + common_line_end,
                                           native_before.st_size, metrics,
                                           allow_nonsettings=True)
        except SharedStoreError as error:
            raise SharedStoreError(f"分页历史正文已分叉，不能零导入切换：{tid}（{error}）") from error
        if native_events is None:
            events = managed_events
            tail_bytes = managed_body - common_line_end
            divergent_suffixes = {"native_nonsettings_suffix": True,
                                  "managed_settings_suffix_events": managed_events}
        else:
            events = managed_events - native_events
            tail_bytes = managed_body - native_body
            divergent_suffixes = {"native_settings_suffix_events": native_events,
                                  "managed_settings_suffix_events": managed_events}
    elif diverged:
        raise SharedStoreError(f"分页历史正文已分叉，不能零导入切换：{tid}")
    elif check_tail and managed_body > native_body:
        events = _settings_tail(managed_path, len(managed_line) + native_body, managed_before.st_size, metrics)
    native_after, managed_after = native_path.stat(), managed_path.stat()
    if (native_before.st_ino != native_after.st_ino or native_after.st_size < native_before.st_size
            or (native_after.st_size == native_before.st_size
                and native_after.st_mtime_ns != native_before.st_mtime_ns)
            or (managed_before.st_ino, managed_before.st_size, managed_before.st_mtime_ns)
            != (managed_after.st_ino, managed_after.st_size, managed_after.st_mtime_ns)):
        raise SharedStoreError(f"检查期间分页 rollout 发生改写或副本仍在写入：{tid}")
    return {"thread_id": tid, "history_base": base,
            "physical_prefix_bytes": common_line_end if diverged else compared,
            "settings_tail_events": events,
            "settings_tail_bytes": tail_bytes,
            **divergent_suffixes}


def preflight(root: Path, source_home: Path, sqlite_home: Path | None = None) -> dict:
    """Approve only a zero-import switch; refuse managed-only work.

    Reads thread/project/turn IDs, projection metadata, and managed item key/
    revision metadata through a read-only indexed join. It never reads item_json
    or copies databases. Rewritten paginated rollouts and ordinary rollouts with
    differing projections receive a direct common-prefix comparison; ordinary
    equal-projection bodies are not reread solely for this validation.
    Call while managed writers are stopped; the native application may continue.
    """
    started = time.monotonic()
    root = Path(root).resolve()
    paths = canonical_paths(source_home, sqlite_home)
    managed_sqlite = root / "shared" / "sqlite"
    if paths["sqlite_home"] == managed_sqlite.resolve():
        raise SharedStoreError("权威历史库不能仍是工具的旧副本。")
    native_threads, native_projects = _metadata(paths["sqlite_home"], True)
    managed_threads, managed_projects = _metadata(managed_sqlite, False)
    native_turns, native_projection = _history(paths["sqlite_home"], True)
    managed_turns, managed_projection = _history(managed_sqlite, False)
    report = {
        "can_switch": False, "source_home": str(paths["source_home"]),
        "sqlite_home": str(paths["sqlite_home"]),
        "native_counts": {"threads": len(native_threads), "projects": len(native_projects),
                          "turns": len(native_turns)},
        "managed_counts": {"threads": len(managed_threads), "projects": len(managed_projects),
                           "turns": len(managed_turns)},
        "managed_only": {"threads": sorted(managed_threads.keys() - native_threads.keys()),
                         "projects": sorted(managed_projects - native_projects),
                         "turns": [list(key) for key in sorted(managed_turns - native_turns)]},
        "settings_only_tails": [],
        "paginated_checks": [],
        "projection_prefix_checks": [],
        "read_only_observation": True, "requires_stopped_managed_apps": True,
        "scope": "history_data_compatibility", "proves_realtime_ui_sync": False,
        "automatic_switch": False,
        "equal_projection_rollout_content_checked": False,
        "bytes_read": 0,
    }
    if any(report["managed_only"].values()):
        report["rollout_bytes_read"] = report.pop("bytes_read")
        report["elapsed_seconds"] = round(time.monotonic() - started, 6)
        raise SharedStoreError("工具副本有独有会话、项目或 turn；先迁移这些工作，不能零导入切换。", report)
    owned_rollout_roots = [(root / "shared" / name).resolve()
                          for name in ("sessions", "archived_sessions")]
    try:
        _check_all_item_versions(paths["sqlite_home"], managed_sqlite)
        for tid, thread in managed_threads.items():
            path = Path(thread["rollout_path"]).resolve()
            if not any(path.is_relative_to(directory) for directory in owned_rollout_roots):
                raise SharedStoreError(f"旧副本 rollout 指向受管历史外：{tid}")
            if tid not in managed_projection or tid not in native_projection:
                raise SharedStoreError(f"缺少会话 projection，不能确认零导入：{tid}")
            managed_offset, managed_ordinal = managed_projection[tid]
            native_offset, native_ordinal = native_projection[tid]
            size = path.stat().st_size
            if size != managed_offset and thread["history_mode"] == "paginated":
                check = {"thread_id": tid, "projection_offset": managed_offset,
                         "rollout_size": size, "verified": False}
                report["paginated_checks"].append(check)
                native_thread = native_threads[tid]
                if native_thread["history_mode"] != "paginated":
                    raise SharedStoreError(f"两侧 history_mode 不同，不能确认分页基线：{tid}")
                with _native_rollout(Path(native_thread["rollout_path"])) as native_path:
                    check.update(_rollout_prefix(native_path, path, tid,
                        native_projection[tid], managed_projection[tid], size < managed_offset, check))
                check.update(_check_versions(paths["sqlite_home"], managed_sqlite, tid))
                if check.get("native_nonsettings_suffix"):
                    if native_offset < managed_offset or native_ordinal < managed_ordinal:
                        raise SharedStoreError(f"权威工作续写的 projection 未领先副本：{tid}")
                    report["settings_only_tails"].append({"thread_id": tid,
                        "bytes": check["settings_tail_bytes"],
                        "events": check["settings_tail_events"],
                        "native_has_work": True})
                elif ("native_settings_suffix_events" in check or
                        managed_offset > native_offset or managed_ordinal > native_ordinal) and (
                        managed_offset - native_offset != check["settings_tail_bytes"]
                        or managed_ordinal - native_ordinal != check["settings_tail_events"]):
                    raise SharedStoreError(f"分页副本领先的 projection 无法由设置尾部解释：{tid}")
                check["verified"] = True
                continue
            if min(managed_offset, native_offset, managed_ordinal, native_ordinal) < 0 or size < managed_offset:
                raise SharedStoreError(f"会话 projection 与 rollout 不一致：{tid}")
            if (managed_offset, managed_ordinal) != (native_offset, native_ordinal):
                # A projection difference is a bounded reason to compare the
                # common bytes. The provider header was normalized at import;
                # only that field may differ without changing the work history.
                native_path = Path(native_threads[tid]["rollout_path"])
                check = {"thread_id": tid, "bytes_read": 0}
                report["projection_prefix_checks"].append(check)
                with _native_rollout(native_path) as readable_path:
                    check.update(_rollout_prefix(readable_path, path, tid,
                                 native_projection[tid], managed_projection[tid], False, check))
                if check.get("native_nonsettings_suffix"):
                    if native_offset < managed_offset or native_ordinal < managed_ordinal:
                        raise SharedStoreError(f"权威工作续写的 projection 未领先副本：{tid}")
                    report["settings_only_tails"].append({"thread_id": tid,
                        "bytes": check["settings_tail_bytes"],
                        "events": check["settings_tail_events"],
                        "native_has_work": True})
                elif ("native_settings_suffix_events" in check or
                        managed_offset > native_offset or managed_ordinal > native_ordinal):
                    if (managed_offset - native_offset != check["settings_tail_bytes"]
                            or managed_ordinal - native_ordinal != check["settings_tail_events"]):
                        raise SharedStoreError(f"设置尾部与 projection 不一致：{tid}")
                    report["settings_only_tails"].append({"thread_id": tid,
                        "bytes": check["settings_tail_bytes"],
                        "events": check["settings_tail_events"],
                        "divergent": "native_settings_suffix_events" in check})
            if managed_offset == native_offset and managed_ordinal != native_ordinal:
                raise SharedStoreError(f"设置尾部与 projection ordinal 不一致：{tid}")
            # A stopped copy can contain committed rollout events that were
            # never indexed. Those must also be checked rather than discarded.
            tail_start = max(native_offset, managed_offset)
            if size > tail_start:
                events = _settings_tail(path, tail_start, size, report)
                report["settings_only_tails"].append({"thread_id": tid,
                    "bytes": size - tail_start, "events": events, "unprojected": True})
    except (OSError, SharedStoreError) as error:
        report["rollout_bytes_read"] = report.pop("bytes_read") + sum(
            check.get("bytes_read", 0) for check in
            report["paginated_checks"] + report["projection_prefix_checks"])
        report["elapsed_seconds"] = round(time.monotonic() - started, 6)
        raise SharedStoreError(str(error), report) from error
    report["can_switch"] = True
    report["rollout_bytes_read"] = report.pop("bytes_read") + sum(
        check.get("bytes_read", 0) for check in
        report["paginated_checks"] + report["projection_prefix_checks"])
    report["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return report


def _managed_home(root: Path, home: Path) -> tuple[Path, Path]:
    root = Path(root).resolve()
    home = Path(home).absolute()
    relative = home.relative_to(root) if home.is_relative_to(root) else None
    if (relative is None or len(relative.parts) != 3 or relative.parts[0] != "profiles"
            or relative.parts[2] != "codex-home" or home.is_symlink()
            or not home.is_dir() or not home.resolve().is_relative_to(root)):
        raise SharedStoreError("历史链接目标必须是工具受管 profile 的 codex-home。")
    return root, home


def _resolved_target(link: Path, target: str) -> Path:
    target_path = Path(target)
    return (target_path if target_path.is_absolute() else link.parent / target_path).resolve()


def _link_state(root: Path, home: Path, paths: dict) -> dict:
    originals = {}
    targets = {}
    for name in HISTORY_DIRS:
        link = home / name
        old = (root / "shared" / name).resolve()
        new = paths[name]
        if not old.is_relative_to(root) or not new.is_dir():
            raise SharedStoreError(f"历史目录不符合受管/权威范围：{name}")
        if not link.is_symlink() or link.resolve() not in (old, new):
            raise SharedStoreError(f"拒绝覆盖陌生历史目录或链接：{link}")
        originals[name] = os.readlink(link)
        targets[name] = str(new)
    return {"root": str(root), "home": str(home),
            "source_home": str(paths["source_home"]), "sqlite_home": str(paths["sqlite_home"]),
            "originals": originals, "targets": targets}


def _replace_link(link: Path, target: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".history-link-", dir=link.parent)
    os.close(descriptor)
    os.unlink(temporary)
    try:
        Path(temporary).symlink_to(target, target_is_directory=True)
        os.replace(temporary, link)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _record_path(root: Path, home: Path) -> Path:
    path = root / (".history-links-" + home.parent.name + ".json")
    if path.is_symlink():
        raise SharedStoreError("历史恢复记录不得是外部符号链接。")
    return path


def _save_record(path: Path, record: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".history-record-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate_record(root: Path, home: Path, record: dict) -> None:
    if record.get("root") != str(root) or record.get("home") != str(home):
        raise SharedStoreError("历史恢复记录与受管 home 不匹配。")
    paths = canonical_paths(Path(record["source_home"]), Path(record["sqlite_home"]))
    for name in HISTORY_DIRS:
        link = home / name
        old = (root / "shared" / name).resolve()
        original = record["originals"][name]
        target = record["targets"][name]
        if (not old.is_relative_to(root) or target != str(paths[name])
                or _resolved_target(link, original) not in (old, paths[name])):
            raise SharedStoreError("历史恢复记录包含陌生目录。")
        if not link.is_symlink() or link.resolve() not in (old, paths[name]):
            raise SharedStoreError(f"拒绝覆盖已被更改的历史链接：{link}")


def attach_home(root: Path, home: Path, source_home: Path,
                sqlite_home: Path | None = None) -> dict:
    """Attach three history links after preflight/stopping, preserving originals.

    The rollback record is saved in the owned root before the first link change.
    This function neither edits sqlite_home config nor runs/stops applications.
    Repeated calls preserve the first original symlink targets.
    """
    root, home = _managed_home(root, home)
    paths = canonical_paths(source_home, sqlite_home)
    if paths["source_home"].is_relative_to(root) or root.is_relative_to(paths["source_home"]):
        raise SharedStoreError("权威 Codex home 与工具目录不能重叠。")
    current = _link_state(root, home, paths)
    journal = _record_path(root, home)
    if journal.exists():
        record = json.loads(journal.read_text())
        _validate_record(root, home, record)
        if record["targets"] != current["targets"] or record["sqlite_home"] != current["sqlite_home"]:
            raise SharedStoreError("已有另一权威目录的历史恢复记录，请先恢复。")
    else:
        record = current
        _save_record(journal, record)
    changed = []
    try:
        for name in HISTORY_DIRS:
            link = home / name
            if link.resolve() != Path(record["targets"][name]):
                _replace_link(link, record["targets"][name])
                changed.append(name)
    except OSError:
        for name in reversed(changed):
            _replace_link(home / name, current["originals"][name])
        raise
    return record


def restore_home(root: Path, home: Path, record: dict | None = None) -> dict:
    """Restore recorded symlink targets; leave canonical/old data untouched."""
    root, home = _managed_home(root, home)
    if record is None:
        record = json.loads(_record_path(root, home).read_text())
    _validate_record(root, home, record)
    current = {name: os.readlink(home / name) for name in HISTORY_DIRS}
    changed = []
    try:
        for name in HISTORY_DIRS:
            if current[name] != record["originals"][name]:
                _replace_link(home / name, record["originals"][name])
                changed.append(name)
    except OSError:
        for name in reversed(changed):
            _replace_link(home / name, current[name])
        raise
    return {"restored": changed, "home": str(home)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读检查历史数据兼容性；不证明实时同步，不自动切换链接、配置或进程。")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-home", type=Path, required=True)
    parser.add_argument("--sqlite-home", type=Path)
    arguments = parser.parse_args(argv)
    try:
        report = preflight(arguments.root, arguments.source_home, arguments.sqlite_home)
    except SharedStoreError as error:
        report = {**error.report, "can_switch": False, "read_only_observation": True,
                  "scope": "history_data_compatibility", "proves_realtime_ui_sync": False,
                  "automatic_switch": False,
                  "error": str(error)}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    except (OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps({"can_switch": False, "read_only_observation": True,
                          "scope": "history_data_compatibility", "proves_realtime_ui_sync": False,
                          "automatic_switch": False,
                          "error": str(error)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
