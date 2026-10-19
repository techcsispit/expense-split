"""Concurrency regression and integration tests for GitHub issue #35."""

import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from split import io
from split.cli import (
    cmd_add_expense,
    cmd_delete_expense,
    cmd_edit_expense,
    cmd_settle,
)


class TestConcurrentGroupSaves(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.filepath = Path(self._tmp.name) / "groups.json"

    def _write_groups(self, groups):
        self.filepath.write_text(json.dumps(groups, indent=2), encoding="utf-8")

    def _run_concurrently(self, operations):
        """Make all commands load first, then allow their saves to race."""
        barrier = threading.Barrier(len(operations))

        def synchronized_load(group_id):
            group = copy.deepcopy(io.load_group(group_id, self.filepath))
            barrier.wait(timeout=5)
            return group

        errors = []

        def run(operation):
            try:
                operation()
            except BaseException as exc:
                errors.append(exc)

        with patch("split.cli.load_group", side_effect=synchronized_load), \
                patch("split.cli.save_group", wraps=io.save_group) as save, \
                patch("builtins.print"):
            save.side_effect = lambda group_id, group: io.save_group(
                group_id, group, self.filepath
            )
            threads = [threading.Thread(target=run, args=(op,)) for op in operations]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        return io.load_all_groups(self.filepath)

    def test_concurrent_save_group_preserves_unrelated_updates(self):
        """Two successful saves based on the same snapshot must both persist."""
        self._write_groups({"g1": {"v": 0}, "g2": {"v": 0}})

        first_write_started = threading.Event()
        release_first_write = threading.Event()
        real_atomic_write = io._atomic_write_json
        call_count = 0
        count_guard = threading.Lock()

        def pause_first_write(data, filepath):
            nonlocal call_count
            with count_guard:
                call_count += 1
                current_call = call_count
            if current_call == 1:
                first_write_started.set()
                self.assertTrue(release_first_write.wait(timeout=5))
            real_atomic_write(data, filepath)

        errors = []

        def save(group_id):
            try:
                io.save_group(group_id, {"v": 1}, self.filepath)
            except BaseException as exc:
                errors.append(exc)

        with patch("split.io._atomic_write_json", side_effect=pause_first_write):
            first = threading.Thread(target=save, args=("g1",))
            second = threading.Thread(target=save, args=("g2",))
            first.start()
            self.assertTrue(first_write_started.wait(timeout=5))
            second.start()
            self.assertFalse(release_first_write.is_set())
            release_first_write.set()
            first.join(timeout=10)
            second.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(
            io.load_all_groups(self.filepath),
            {"g1": {"v": 1}, "g2": {"v": 1}},
        )

    def test_database_lock_serializes_separate_processes(self):
        """The sidecar lock coordinates independent CLI processes, not just threads."""
        self._write_groups({"g1": {"v": 0}, "g2": {"v": 0}})
        holder_code = (
            "import sys\n"
            "from split.io import _exclusive_database_lock\n"
            "with _exclusive_database_lock(sys.argv[1]):\n"
            " print('locked', flush=True)\n"
            " sys.stdin.readline()\n"
        )
        saver_code = (
            "import sys\n"
            "from split.io import save_group\n"
            "save_group('g2', {'v': 1}, sys.argv[1])\n"
        )

        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(self.filepath)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            saver = subprocess.Popen(
                [sys.executable, "-c", saver_code, str(self.filepath)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                time.sleep(0.2)
                self.assertIsNone(
                    saver.poll(), "second process bypassed the database lock"
                )
                holder.stdin.write("release\n")
                holder.stdin.flush()
                holder_stdout, holder_stderr = holder.communicate(timeout=5)
                saver_stdout, saver_stderr = saver.communicate(timeout=5)
                self.assertEqual(holder.returncode, 0, holder_stdout + holder_stderr)
                self.assertEqual(saver.returncode, 0, saver_stdout + saver_stderr)
            finally:
                if saver.poll() is None:
                    saver.kill()
                    saver.communicate()
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.communicate()

        self.assertEqual(io.load_all_groups(self.filepath)["g2"], {"v": 1})

    def test_concurrent_add_and_settle_preserve_both_groups(self):
        """The add-expense and settlement output paths share safe persistence."""
        self._write_groups({
            "add": {
                "members": ["alice", "bob"],
                "expenses": [],
                "settlements": [],
            },
            "settle": {
                "members": ["alice", "bob"],
                "expenses": [{
                    "id": 1,
                    "description": "Dinner",
                    "amount": 100.0,
                    "paid_by": "bob",
                    "split": "equal",
                }],
                "settlements": [],
            },
        })

        add_args = SimpleNamespace(
            group="add", description="Lunch", amount="40", paid_by="alice",
            split="equal", share_weights=[],
        )
        settle_args = SimpleNamespace(
            group="settle", payer="alice", receiver="bob", amount=None,
        )
        groups = self._run_concurrently([
            lambda: cmd_add_expense(add_args),
            lambda: cmd_settle(settle_args),
        ])

        self.assertEqual(groups["add"]["expenses"][0]["description"], "Lunch")
        self.assertEqual(groups["settle"]["settlements"], [
            {"from": "alice", "to": "bob", "amount": 50.0}
        ])

    def test_concurrent_edit_and_delete_preserve_full_records(self):
        """Editing uses ID identity; deleting removes only the located record."""
        original_a = {
            "members": ["alice", "bob"],
            "expenses": [{
                "id": 7, "description": "Old", "amount": 20.0,
                "paid_by": "alice", "split": "equal",
            }],
            "settlements": [],
        }
        original_b = {
            "members": ["alice", "bob"],
            "expenses": [
                {"id": 7, "description": "Keep", "amount": 20.0,
                 "paid_by": "alice", "split": "equal"},
                {"id": 8, "description": "Delete", "amount": 30.0,
                 "paid_by": "bob", "split": "equal"},
            ],
            "settlements": [],
        }
        self._write_groups({"edit": original_a, "delete": original_b})

        edit_args = SimpleNamespace(
            group="edit", expense_id="7", description="New", amount="25",
            paid_by="bob", split="equal", share_weights=[],
        )
        delete_args = SimpleNamespace(group="delete", expense_id="8")
        groups = self._run_concurrently([
            lambda: cmd_edit_expense(edit_args),
            lambda: cmd_delete_expense(delete_args),
        ])

        self.assertEqual(groups["edit"]["expenses"], [{
            "id": 7, "description": "New", "amount": 25.0,
            "paid_by": "bob", "split": "equal",
        }])
        self.assertEqual(groups["delete"]["expenses"], [
            original_b["expenses"][0]
        ])


if __name__ == "__main__":
    unittest.main()
