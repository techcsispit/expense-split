"""Atomic JSON persistence tests for split.io.

These tests exercise save_group's atomic write behavior without touching the
repository's committed data/groups.json. All tests operate on isolated files in
temporary directories. Failure injection is done with targeted mocking.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from split import io

# Captured before any mock.patch swaps split.io.os.fdopen (the same os.fdopen
# object).  The failure-injecting wrappers below open the real temp file first,
# then wrap it, so the descriptor is always closed when the `with` block exits
# and Windows can unlink the temp file during cleanup.
_REAL_FDOPEN = os.fdopen


class _FailingFile:
    """Wrap a real text file and inject a failure at a chosen operation.

    Everything else -- including the context-manager protocol -- is delegated to
    the wrapped file so its descriptor is always closed on exit, letting the
    atomic writer remove the temp file even when an exception is raised.
    """

    def __init__(self, real_file, fail_on=None):
        self._real = real_file
        self._fail_on = fail_on

    def write(self, data):
        if self._fail_on == "write":
            raise OSError("disk full")
        return self._real.write(data)

    def flush(self):
        if self._fail_on == "flush":
            raise OSError("flush failed")
        return self._real.flush()

    def fileno(self):
        return self._real.fileno()

    def close(self):
        return self._real.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._real.close()
        return False


def _fdopen_with_failure(fail_on):
    """Return an os.fdopen replacement that injects a failure after opening."""

    def _factory(fd, mode, **kwargs):
        return _FailingFile(_REAL_FDOPEN(fd, mode, **kwargs), fail_on=fail_on)

    return _factory


class TestSaveGroupAtomicBehavior(unittest.TestCase):
    """Regression tests for atomic JSON persistence (GitHub issue #26)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.filepath = self.dir / "groups.json"

    def _write_original(self):
        self.filepath.write_text('{"g1": {"name": "Friends"}}', encoding="utf-8")
        return self.filepath.read_bytes()

    # ------------------------------------------------------------------ #
    # 1. Successful saves create valid JSON.
    # ------------------------------------------------------------------ #
    def test_successful_save_creates_valid_json(self):
        io.save_group("g1", {"name": "Friends"}, filepath=self.filepath)
        data = io.load_all_groups(self.filepath)
        self.assertEqual(data["g1"], {"name": "Friends"})

    def test_successful_save_keeps_indent_and_utf8(self):
        io.save_group("g1", {"name": "Friends"}, filepath=self.filepath)
        raw = self.filepath.read_bytes()
        # The file is serialized with json.dump(indent=2), so the first line is
        # "{" and the key follows on the next line.  Line endings may be LF or
        # CRLF depending on the platform, so normalize before asserting.
        self.assertIn(b'"g1":', raw.replace(b"\r\n", b"\n"))
        self.assertEqual(
            io.load_all_groups(self.filepath), {"g1": {"name": "Friends"}}
        )


    # ------------------------------------------------------------------ #
    # 2. Updating one group preserves all other groups.
    # ------------------------------------------------------------------ #
    def test_updating_one_group_preserves_others(self):
        groups = {
            "g1": {"name": "Friends"},
            "g2": {"name": "Family", "members": ["a", "b"]},
        }
        self.filepath.write_text(json.dumps(groups, indent=2), encoding="utf-8")
        io.save_group("g1", {"name": "Friends", "extra": True}, filepath=self.filepath)
        data = io.load_all_groups(self.filepath)
        self.assertEqual(data["g1"]["extra"], True)
        self.assertEqual(data["g2"], {"name": "Family", "members": ["a", "b"]})

    # ------------------------------------------------------------------ #
    # 3. Saving to a nonexistent destination creates the file correctly.
    # ------------------------------------------------------------------ #
    def test_save_to_nonexistent_file_creates_it(self):
        new_path = self.dir / "new" / "deep" / "groups.json"
        io.save_group("g1", {"name": "Friends"}, filepath=new_path)
        self.assertTrue(new_path.exists())
        self.assertEqual(
            io.load_all_groups(new_path), {"g1": {"name": "Friends"}}
        )

    # ------------------------------------------------------------------ #
    # 4. A JSON serialization failure leaves the original file unchanged.
    # ------------------------------------------------------------------ #
    def test_serialization_failure_leaves_original_unchanged(self):
        self.filepath.write_text('{"g1": {"name": "Friends"}}', encoding="utf-8")
        original_bytes = self.filepath.read_bytes()

        class Unserializable:
            def __repr__(self):
                return "unserializable"

        with mock.patch("split.io.json.dump", side_effect=TypeError("serialize fail")):
            with self.assertRaises(TypeError):
                io.save_group("g1", Unserializable(), filepath=self.filepath)

        self.assertEqual(self.filepath.read_bytes(), original_bytes)
    # ------------------------------------------------------------------ #
    # 5. A simulated temporary-file write failure preserves the original.
    # ------------------------------------------------------------------ #
    def test_tempfile_write_failure_preserves_original(self):
        original_bytes = self._write_original()

        with mock.patch(
            "split.io.os.fdopen", side_effect=_fdopen_with_failure("write")
        ):
            with self.assertRaises(OSError):
                io.save_group("g1", {"name": "Broken"}, filepath=self.filepath)

        self.assertEqual(self.filepath.read_bytes(), original_bytes)

    # ------------------------------------------------------------------ #
    # 6. A simulated flush failure preserves the original file.
    # ------------------------------------------------------------------ #
    def test_flush_failure_preserves_original(self):
        original_bytes = self._write_original()

        with mock.patch(
            "split.io.os.fdopen", side_effect=_fdopen_with_failure("flush")
        ):
            with self.assertRaises(OSError):
                io.save_group("g1", {"name": "Broken"}, filepath=self.filepath)

        self.assertEqual(self.filepath.read_bytes(), original_bytes)

    # ------------------------------------------------------------------ #
    # 6b. A simulated fsync failure preserves the original file.
    # ------------------------------------------------------------------ #
    def test_fsync_failure_preserves_original(self):
        original_bytes = self._write_original()

        with mock.patch("split.io.os.fsync", side_effect=OSError("fsync failed")):
            with self.assertRaises(OSError):
                io.save_group("g1", {"name": "Broken"}, filepath=self.filepath)

        self.assertEqual(self.filepath.read_bytes(), original_bytes)

    # ------------------------------------------------------------------ #
    # 7. A simulated os.replace failure preserves the original file.
    # ------------------------------------------------------------------ #
    def test_os_replace_failure_preserves_original(self):
        original_bytes = self._write_original()

        with mock.patch(
            "split.io.os.replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaises(OSError):
                io.save_group("g1", {"name": "Broken"}, filepath=self.filepath)

        self.assertEqual(self.filepath.read_bytes(), original_bytes)

    # ------------------------------------------------------------------ #
    # 8. Temporary files are cleaned up after failures.
    # ------------------------------------------------------------------ #
    def test_temp_files_removed_after_failure(self):
        self._write_original()

        with mock.patch(
            "split.io.os.fdopen", side_effect=_fdopen_with_failure("flush")
        ):
            with self.assertRaises(OSError):
                io.save_group("g1", {"name": "Broken"}, filepath=self.filepath)

        expected_names = {self.filepath.name, f"{self.filepath.name}.lock"}
        leftovers = [p for p in self.dir.iterdir() if p.name not in expected_names]
        self.assertEqual(leftovers, [])

    # ------------------------------------------------------------------ #
    # 9. Repeated successful updates remain readable by load_all_groups()
    #    and load_group().
    # ------------------------------------------------------------------ #
    def test_repeated_updates_are_readable(self):
        for i in range(5):
            io.save_group("g1", {"counter": i}, filepath=self.filepath)
        self.assertEqual(io.load_all_groups(self.filepath)["g1"], {"counter": 4})
        self.assertEqual(io.load_group("g1", filepath=self.filepath), {"counter": 4})

    # ------------------------------------------------------------------ #
    # 10. Existing behavior and function signatures remain compatible.
    # ------------------------------------------------------------------ #
    def test_existing_default_path_behavior(self):
        # Loading the committed default DB still works and is read-only here.
        loaded = io.load_all_groups()
        self.assertIsInstance(loaded, dict)

    def test_save_group_signature(self):
        import inspect

        sig = inspect.signature(io.save_group)
        self.assertEqual(list(sig.parameters), ["group_id", "group_data", "filepath"])
        self.assertEqual(sig.parameters["filepath"].default, io.DEFAULT_DATA_PATH)

    def test_load_group_interface(self):
        self.assertIsNone(io.load_group("missing", filepath=self.filepath))
        self.filepath.write_text('{"g1": {"name": "Friends"}}', encoding="utf-8")
        self.assertEqual(io.load_group("g1", filepath=self.filepath), {"name": "Friends"})

