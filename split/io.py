"""File I/O operations for groups and expense records."""

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "groups.json"

_thread_locks = {}
_thread_locks_guard = threading.Lock()


def _thread_lock_for(path):
    """Return the in-process lock associated with a database path."""
    resolved_path = Path(path).resolve()
    with _thread_locks_guard:
        return _thread_locks.setdefault(resolved_path, threading.Lock())


@contextmanager
def _exclusive_database_lock(filepath):
    """Serialize read-modify-write transactions for one JSON database.

    A thread lock handles callers in this process, while a one-byte OS lock on
    a stable sidecar file coordinates separate CLI processes.  The sidecar is
    intentionally retained: deleting and recreating it could let contenders
    lock different file identities at the same time.
    """
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")

    with _thread_lock_for(path):
        with open(lock_path, "a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()

            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

            try:
                yield
            finally:
                lock_file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_all_groups(filepath=DEFAULT_DATA_PATH):
    """Loads all group data from JSON."""
    path = Path(filepath)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_group(group_id, filepath=DEFAULT_DATA_PATH):
    """Loads data for a specific group. Returns None if group not found."""
    groups = load_all_groups(filepath)
    return groups.get(group_id)


def _atomic_write_json(data, filepath):
    """Atomically serialize `data` to `filepath` as JSON.

    The updated database is written to a temporary file in the same directory
    as the destination, flushed, and synced to disk before the destination is
    replaced with :func:`os.replace`. If serialization, writing, flushing,
    syncing, or replacement fails, the original file is left untouched and the
    temporary file is removed, so the previous database is preserved.
    """
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".groups_tmp-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # Remove the temporary file without masking the original exception.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_group(group_id, group_data, filepath=DEFAULT_DATA_PATH):
    """Saves or updates a specific group in the JSON store."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_database_lock(path):
        groups = load_all_groups(path)
        groups[group_id] = group_data
        _atomic_write_json(groups, path)
