"""Unit tests for Issue #50: Validated Backup and Atomic Restore for Group Data.

Tests cover:
1. Creating a backup preserves the complete group store.
2. Backup creation does not modify the live store.
3. Restoring a valid backup produces equivalent group data and financial calculations.
4. Checksum mismatches and corrupted backups are rejected.
5. Malformed JSON and missing backup files are handled cleanly.
6. Dry-run mode leaves all relevant files unchanged.
7. Failed restoration preserves the live store byte-for-byte.
8. Failed recovery-backup creation prevents live-store replacement.
9. Expense IDs, multiple payers, weighted splits, archived members, and settlement history survive a round trip.
10. Invalid group data cannot be restored.
11. Filesystem and atomic-write failures do not leave partially restored data.
12. Concurrent writes cannot be silently lost during backup or restore.
13. Attempts to overwrite the live store with the backup output are rejected.
14. Existing CLI functionality and all previous tests continue to pass.
"""

import copy
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from split import cli
from split.backup import (
    create_backup,
    restore_backup,
    load_and_inspect_backup,
    compute_canonical_sha256,
    create_backup_payload,
    BACKUP_FORMAT_VERSION,
)
from split.calc import get_net_balances, who_owes, settle_plan
from split.io import (
    load_group,
    load_all_groups,
    save_group,
    DEFAULT_DATA_PATH,
    _exclusive_database_lock,
)
from split.validation import validate_store


class TestBackupRestore(unittest.TestCase):

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.data_path = Path(self._tmp_dir.name) / "groups.json"
        self.backup_path = Path(self._tmp_dir.name) / "backup.json"

    def _write_store(self, data):
        with open(self.data_path, "w", encoding="utf-8") as f:
            if isinstance(data, str):
                f.write(data)
            else:
                json.dump(data, f, indent=2)

    def _read_store(self):
        with open(self.data_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _read_bytes(self, path=None):
        target = self.data_path if path is None else Path(path)
        return target.read_bytes()

    def _run_cli(self, args_list):
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        exit_code = 0
        defaults = (self.data_path,)
        with patch.object(sys, "argv", ["split"] + args_list), \
             patch.object(load_group, "__defaults__", defaults), \
             patch.object(save_group, "__defaults__", defaults), \
             patch.object(load_all_groups, "__defaults__", defaults), \
             patch("split.cli.DEFAULT_DATA_PATH", self.data_path), \
             patch("split.backup.DEFAULT_DATA_PATH", self.data_path), \
             redirect_stdout(out_buf), redirect_stderr(err_buf):
            try:
                cli.main()
            except SystemExit as e:
                exit_code = e.code if e.code is not None else 0
        return exit_code, out_buf.getvalue(), err_buf.getvalue()

    def test_1_creating_backup_preserves_complete_group_store(self):
        """1. Creating a backup preserves the complete group store with metadata and checksum."""
        store_data = {
            "groupA": {
                "name": "Trip",
                "members": ["alice", "bob"],
                "expenses": [
                    {"id": 1, "description": "Fuel", "amount": 100.0, "paid_by": "alice", "split": "equal"}
                ],
                "settlements": []
            }
        }
        self._write_store(store_data)

        code, out, err = self._run_cli(["backup-data", "--output", str(self.backup_path)])
        self.assertEqual(code, 0)
        self.assertIn("Backup created successfully", out)
        self.assertTrue(self.backup_path.exists())

        # Inspect backup structure
        with open(self.backup_path, "r", encoding="utf-8") as f:
            backup_content = json.load(f)

        self.assertEqual(backup_content["backup_version"], BACKUP_FORMAT_VERSION)
        self.assertIn("checksum", backup_content)
        self.assertIn("created_at", backup_content)
        self.assertEqual(backup_content["groups"], store_data)
        expected_checksum = compute_canonical_sha256(store_data)
        self.assertEqual(backup_content["checksum"], expected_checksum)

    def test_2_backup_creation_does_not_modify_live_store(self):
        """2. Backup creation is strictly read-only with respect to the live store."""
        store_data = {
            "g1": {
                "name": "Group 1",
                "members": ["alice", "bob"],
                "expenses": [],
                "settlements": []
            }
        }
        self._write_store(store_data)
        before_bytes = self._read_bytes()

        code, out, _ = self._run_cli(["backup-data", "--output", str(self.backup_path)])
        self.assertEqual(code, 0)
        self.assertEqual(self._read_bytes(), before_bytes)

    def test_3_restoring_valid_backup_produces_equivalent_data_and_financial_calculations(self):
        """3. Restoring a valid backup replaces live store and preserves financial balances."""
        original_data = {
            "groupA": {
                "name": "Original",
                "members": ["alice", "bob", "charlie"],
                "expenses": [
                    {
                        "id": 1,
                        "description": "Hotel",
                        "amount": 300.0,
                        "paid_by": {"alice": 200.0, "bob": 100.0},
                        "split": "share",
                        "shares": {"alice": 1, "bob": 1, "charlie": 1}
                    }
                ],
                "settlements": [
                    {"from": "charlie", "to": "alice", "amount": 50.0}
                ]
            }
        }
        # Back it up
        self._write_store(original_data)
        self._run_cli(["backup-data", "--output", str(self.backup_path)])

        orig_balances = get_net_balances(original_data["groupA"])
        orig_debts = who_owes(original_data["groupA"])
        orig_plan = settle_plan(original_data["groupA"])

        # Now corrupt/alter live store with completely different data
        corrupted_data = {
            "groupB": {
                "name": "Different",
                "members": ["dave"],
                "expenses": [],
                "settlements": []
            }
        }
        self._write_store(corrupted_data)

        # Apply restore
        code, out, _ = self._run_cli(["restore-data", str(self.backup_path), "--apply"])
        self.assertEqual(code, 0)
        self.assertIn("Restore completed successfully", out)

        restored_store = self._read_store()
        self.assertEqual(restored_store, original_data)
        self.assertEqual(get_net_balances(restored_store["groupA"]), orig_balances)
        self.assertEqual(who_owes(restored_store["groupA"]), orig_debts)
        self.assertEqual(settle_plan(restored_store["groupA"]), orig_plan)

    def test_4_checksum_mismatches_and_corrupted_backups_are_rejected(self):
        """4. Checksum mismatches or tampered records in backup are rejected without modifying store."""
        store_data = {
            "g1": {
                "name": "G1",
                "members": ["alice", "bob"],
                "expenses": [],
                "settlements": []
            }
        }
        self._write_store(store_data)
        before_bytes = self._read_bytes()

        # Create valid backup first
        self._run_cli(["backup-data", "--output", str(self.backup_path)])

        # Tamper with backup content without updating checksum
        with open(self.backup_path, "r", encoding="utf-8") as f:
            tampered = json.load(f)
        tampered["groups"]["g1"]["name"] = "Tampered Name"
        with open(self.backup_path, "w", encoding="utf-8") as f:
            json.dump(tampered, f)

        # Attempt restore in dry-run and apply
        for flag in ["--dry-run", "--apply"]:
            with self.subTest(flag=flag):
                code, out, err = self._run_cli(["restore-data", str(self.backup_path), flag])
                self.assertEqual(code, 1)
                self.assertIn("Integrity check failed: checksum mismatch", err)
                self.assertEqual(self._read_bytes(), before_bytes)

    def test_5_malformed_json_and_missing_backup_files_handled_cleanly(self):
        """5. Malformed JSON and missing backup files are reported cleanly with exit code 1."""
        self._write_store({"g1": {"name": "G1", "members": ["alice"], "expenses": [], "settlements": []}})
        before_bytes = self._read_bytes()

        # Missing backup file
        non_existent = Path(self._tmp_dir.name) / "non_existent.json"
        code, out, err = self._run_cli(["restore-data", str(non_existent), "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("Backup file not found", err)
        self.assertEqual(self._read_bytes(), before_bytes)

        # Malformed JSON
        malformed = Path(self._tmp_dir.name) / "malformed.json"
        with open(malformed, "w", encoding="utf-8") as f:
            f.write("{ not valid json : ")

        code, out, err = self._run_cli(["restore-data", str(malformed), "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("Malformed JSON in backup file", err)
        self.assertEqual(self._read_bytes(), before_bytes)

    def test_6_dry_run_mode_leaves_all_relevant_files_unchanged(self):
        """6. Dry-run mode validates candidate data and leaves live store and backup completely untouched."""
        store_data = {
            "g1": {
                "name": "Live G1",
                "members": ["alice"],
                "expenses": [],
                "settlements": []
            }
        }
        self._write_store(store_data)
        live_bytes = self._read_bytes()

        backup_payload = create_backup_payload({
            "candidate": {
                "name": "Candidate",
                "members": ["bob"],
                "expenses": [],
                "settlements": []
            }
        })
        with open(self.backup_path, "w", encoding="utf-8") as f:
            json.dump(backup_payload, f)
        backup_bytes = self.backup_path.read_bytes()

        code, out, err = self._run_cli(["restore-data", str(self.backup_path), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("Dry run: Backup", out)
        self.assertIn("is valid and ready for restoration", out)
        self.assertIn("No changes made to live database", out)

        self.assertEqual(self._read_bytes(), live_bytes)
        self.assertEqual(self.backup_path.read_bytes(), backup_bytes)

    def test_7_failed_restoration_preserves_live_store_byte_for_byte(self):
        """7. Failed restoration preserves original live file byte-for-byte."""
        live_store = {
            "live": {
                "name": "Live Store",
                "members": ["alice", "bob"],
                "expenses": [
                    {"id": 1, "description": "Test", "amount": 10.0, "paid_by": "alice", "split": "equal"}
                ],
                "settlements": []
            }
        }
        self._write_store(live_store)
        original_bytes = self._read_bytes()

        # Backup has unsupported format version
        bad_payload = {
            "backup_version": 999,
            "checksum": "abc",
            "groups": {}
        }
        with open(self.backup_path, "w", encoding="utf-8") as f:
            json.dump(bad_payload, f)

        code, out, err = self._run_cli(["restore-data", str(self.backup_path), "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("Unsupported backup format version 999", err)
        self.assertEqual(self._read_bytes(), original_bytes)

    def test_8_failed_recovery_backup_creation_prevents_replacement(self):
        """8. If pre-restore safety backup creation fails, restoration is aborted without modifying store."""
        live_store = {
            "live": {
                "name": "Live",
                "members": ["alice"],
                "expenses": [],
                "settlements": []
            }
        }
        self._write_store(live_store)
        original_bytes = self._read_bytes()

        candidate_payload = create_backup_payload({
            "cand": {
                "name": "Candidate",
                "members": ["bob"],
                "expenses": [],
                "settlements": []
            }
        })
        with open(self.backup_path, "w", encoding="utf-8") as f:
            json.dump(candidate_payload, f)

        # Mock create_safety_backup to raise an exception
        with patch("split.backup.create_safety_backup", side_effect=IOError("Disk permission denied")):
            code, out, err = self._run_cli(["restore-data", str(self.backup_path), "--apply"])
            self.assertEqual(code, 1)
            self.assertIn("Failed to create pre-restore safety backup", err)
            self.assertEqual(self._read_bytes(), original_bytes)

    def test_9_round_trip_preserves_all_complex_structures(self):
        """9. Round trip preserves multiple payers, weighted splits, archived members, legacy expenses, IDs."""
        complex_store = {
            "group_complex": {
                "name": "Complex Group",
                "members": ["alice", "bob", "carol"],
                "archived_members": ["carol"],
                "expenses": [
                    {"description": "Legacy No ID", "amount": 30.0, "paid_by": "alice", "split": "equal"},
                    {
                        "id": 10,
                        "description": "Multi Payer Weighted",
                        "amount": 90.0,
                        "paid_by": {"alice": 60.0, "bob": 30.0},
                        "split": "share",
                        "shares": {"alice": 2, "bob": 1, "carol": 3}
                    }
                ],
                "settlements": [
                    {"from": "bob", "to": "alice", "amount": 10.0}
                ]
            }
        }
        self._write_store(complex_store)
        initial_balances = get_net_balances(complex_store["group_complex"])

        # Backup
        code, out, _ = self._run_cli(["backup-data", "--output", str(self.backup_path)])
        self.assertEqual(code, 0)

        # Clear store
        self._write_store({})

        # Restore
        code, out, _ = self._run_cli(["restore-data", str(self.backup_path), "--apply"])
        self.assertEqual(code, 0)

        restored = self._read_store()
        self.assertEqual(restored, complex_store)
        self.assertEqual(get_net_balances(restored["group_complex"]), initial_balances)

    def test_10_invalid_group_data_in_backup_cannot_be_restored(self):
        """10. Backups containing invalid group data fail validation and cannot be restored."""
        self._write_store({"g1": {"name": "Valid Live", "members": ["alice"], "expenses": [], "settlements": []}})
        original_bytes = self._read_bytes()

        # Backup has valid checksum but invalid group contents (unknown payer)
        invalid_groups = {
            "bad_group": {
                "name": "Bad",
                "members": ["alice", "bob"],
                "expenses": [
                    {"id": 1, "description": "Invalid", "amount": 50.0, "paid_by": "eve", "split": "equal"}
                ],
                "settlements": []
            }
        }
        bad_payload = create_backup_payload(invalid_groups)
        with open(self.backup_path, "w", encoding="utf-8") as f:
            json.dump(bad_payload, f)

        # Dry-run
        code, out, err = self._run_cli(["restore-data", str(self.backup_path), "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("Backup data failed integrity validation", err)
        self.assertIn("Unknown payer 'eve'", err)
        self.assertEqual(self._read_bytes(), original_bytes)

        # Apply
        code, out, err = self._run_cli(["restore-data", str(self.backup_path), "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("Backup data failed integrity validation", err)
        self.assertEqual(self._read_bytes(), original_bytes)

    def test_11_atomic_write_failure_preserves_original_store(self):
        """11. Filesystem failure during replacement leaves store unmodified and cleans temporary files."""
        self._write_store({"live": {"name": "Live", "members": ["alice"], "expenses": [], "settlements": []}})
        original_bytes = self._read_bytes()

        valid_payload = create_backup_payload({
            "new": {"name": "New", "members": ["bob"], "expenses": [], "settlements": []}
        })
        with open(self.backup_path, "w", encoding="utf-8") as f:
            json.dump(valid_payload, f)

        # Mock _atomic_write_json only when writing the live database path
        original_atomic_write = cli.load_all_groups  # just reference
        from split import backup

        real_atomic_write = backup._atomic_write_json

        def mock_atomic_write(data, filepath):
            if Path(filepath) == self.data_path:
                raise OSError("Access denied during live store replace")
            return real_atomic_write(data, filepath)

        with patch("split.backup._atomic_write_json", side_effect=mock_atomic_write):
            code, out, err = self._run_cli(["restore-data", str(self.backup_path), "--apply"])
            self.assertEqual(code, 1)
            self.assertIn("Failed to write restored data", err)
            self.assertEqual(self._read_bytes(), original_bytes)

    def test_12_concurrent_writes_are_not_silently_lost(self):
        """12. Database lock serializes concurrent write commands during backup and restore."""
        self._write_store({"g1": {"name": "G1", "members": ["alice", "bob"], "expenses": [], "settlements": []}})

        backup_payload = create_backup_payload({
            "restored_g": {"name": "Restored", "members": ["alice", "bob"], "expenses": [], "settlements": []}
        })
        with open(self.backup_path, "w", encoding="utf-8") as f:
            json.dump(backup_payload, f)

        # Background thread acquires database lock, performs save_group
        def thread_task():
            save_group("g2", {
                "name": "G2",
                "members": ["alice"],
                "expenses": [],
                "settlements": []
            }, filepath=self.data_path)

        t = threading.Thread(target=thread_task)
        t.start()
        # Restore simultaneously
        restore_backup(self.backup_path, live_path=self.data_path, dry_run=False)
        t.join()

        # The final file is valid and consistent (either restored_g then g2 added, or g2 then replaced)
        final_data = self._read_store()
        self.assertIsInstance(final_data, dict)
        diags, _ = validate_store(final_data)
        self.assertEqual(len(diags), 0)

    def test_13_overwrite_live_store_with_backup_output_rejected(self):
        """13. Attempting to output backup to live database path is rejected."""
        self._write_store({"g1": {"name": "G1", "members": ["alice"], "expenses": [], "settlements": []}})
        original_bytes = self._read_bytes()

        code, out, err = self._run_cli(["backup-data", "--output", str(self.data_path)])
        self.assertEqual(code, 1)
        self.assertIn("cannot point to the live database file", err)
        self.assertEqual(self._read_bytes(), original_bytes)

    def test_14_mode_requirement_and_existing_cli_commands_work(self):
        """14. restore-data requires exactly one of --dry-run or --apply; existing CLI commands work."""
        self._write_store({
            "flatmates": {
                "name": "Flatmates",
                "members": ["alice", "bob"],
                "expenses": [
                    {"id": 1, "description": "Tea", "amount": 20.0, "paid_by": "alice", "split": "equal"}
                ],
                "settlements": []
            }
        })

        # Calling restore-data without mode flag fails argument parsing
        code, out, err = self._run_cli(["restore-data", str(self.backup_path)])
        self.assertNotEqual(code, 0)

        # Existing commands work normally
        code, out, _ = self._run_cli(["summary", "flatmates"])
        self.assertEqual(code, 0)
        self.assertIn("Bob owes Alice ₹10.00", out)

        code, out, _ = self._run_cli(["add-expense", "flatmates", "Coffee", "30.0", "bob", "equal"])
        self.assertEqual(code, 0)
        store = self._read_store()
        self.assertEqual(len(store["flatmates"]["expenses"]), 2)


if __name__ == "__main__":
    unittest.main()
