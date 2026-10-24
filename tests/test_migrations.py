"""Unit tests for Issue #49: Versioned group schema and safe migration workflow.

Focused tests for:
1. Existing unversioned groups remain readable by standard operations.
2. Supported legacy groups migrate to the current version (v1).
3. Already-migrated groups are not changed unnecessarily (no-op).
4. Migration is deterministic and safe to retry (idempotent).
5. Unsupported future versions are rejected with actionable errors.
6. Invalid source data blocks unsafe migration.
7. Multiple-payer expenses and weighted splits remain unchanged.
8. Expense IDs and legacy expenses without IDs are preserved.
9. Settlement history and archived-member metadata remain unchanged.
10. Dry-run mode does not modify the file.
11. Failed migration leaves the original file byte-for-byte unchanged.
12. Successful migration passes the integrity validator.
13. Concurrent writes are not silently lost (locking coordination).
14. Existing CLI commands continue to pass seamlessly.
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
from split.calc import get_net_balances, who_owes, settle_plan
from split.io import load_group, load_all_groups, save_group, _exclusive_database_lock
from split.migrations import (
    CURRENT_SCHEMA_VERSION,
    execute_migration,
    plan_migration,
    inspect_store_versions,
)
from split.validation import validate_store, validate_file


class TestGroupSchemaAndMigrations(unittest.TestCase):

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.data_path = Path(self._tmp_dir.name) / "groups.json"

    def _write_store(self, data):
        with open(self.data_path, "w", encoding="utf-8") as f:
            if isinstance(data, str):
                f.write(data)
            else:
                json.dump(data, f, indent=2)

    def _read_store(self):
        with open(self.data_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _read_bytes(self):
        return self.data_path.read_bytes()

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
             patch("split.migrations.DEFAULT_DATA_PATH", self.data_path), \
             redirect_stdout(out_buf), redirect_stderr(err_buf):
            try:
                cli.main()
            except SystemExit as e:
                exit_code = e.code if e.code is not None else 0
        return exit_code, out_buf.getvalue(), err_buf.getvalue()

    def test_1_existing_unversioned_groups_remain_readable(self):
        """1. Existing unversioned groups remain readable without requiring migration."""
        legacy_store = {
            "group1": {
                "name": "Trip",
                "members": ["alice", "bob"],
                "expenses": [
                    {"id": 1, "description": "Fuel", "amount": 100.0, "paid_by": "alice", "split": "equal"}
                ],
                "settlements": []
            }
        }
        self._write_store(legacy_store)
        orig_bytes = self._read_bytes()

        # Regular loading via load_group / load_all_groups
        g = load_group("group1", filepath=self.data_path)
        self.assertIsNotNone(g)
        self.assertEqual(g["name"], "Trip")
        self.assertNotIn("schema_version", g)

        # Balances work directly
        balances = get_net_balances(g)
        self.assertEqual(balances["alice"], 5000)
        self.assertEqual(balances["bob"], -5000)

        # CLI summary command works directly and leaves file byte-for-byte unchanged
        code, out, err = self._run_cli(["summary", "group1"])
        self.assertEqual(code, 0)
        self.assertIn("Group: Trip", out)
        self.assertIn("Bob owes Alice ₹50.00", out)
        self.assertEqual(self._read_bytes(), orig_bytes)

    def test_2_supported_legacy_groups_migrate_to_current_version(self):
        """2. Supported legacy groups migrate to the current version (schema_version=1)."""
        legacy_store = {
            "g1": {
                "name": "Group 1",
                "members": ["alice", "bob"],
                "expenses": [
                    {"id": 1, "description": "Food", "amount": 60.0, "paid_by": "alice", "split": "equal"}
                ],
                "settlements": []
            }
        }
        self._write_store(legacy_store)

        code, out, _ = self._run_cli(["migrate-data", "--apply"])
        self.assertEqual(code, 0)
        self.assertIn("Successfully migrated 1 group:", out)
        self.assertIn("legacy (unversioned) -> v1", out)

        migrated_store = self._read_store()
        self.assertEqual(migrated_store["g1"]["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(migrated_store["g1"]["name"], "Group 1")
        self.assertEqual(migrated_store["g1"]["members"], ["alice", "bob"])
        self.assertEqual(migrated_store["g1"]["expenses"][0]["id"], 1)

    def test_3_already_migrated_groups_are_not_changed(self):
        """3. Already-migrated groups are not changed unnecessarily (no-op)."""
        current_store = {
            "g1": {
                "schema_version": 1,
                "name": "Group 1",
                "members": ["alice", "bob"],
                "expenses": [],
                "settlements": []
            }
        }
        self._write_store(current_store)
        orig_bytes = self._read_bytes()

        code, out, _ = self._run_cli(["migrate-data", "--apply"])
        self.assertEqual(code, 0)
        self.assertIn("Store is already up-to-date. No migrations required.", out)
        self.assertEqual(self._read_bytes(), orig_bytes)

    def test_4_migration_is_deterministic_and_safe_to_retry(self):
        """4. Migration is deterministic and safe to retry (idempotent)."""
        legacy_store = {
            "g1": {
                "name": "Group 1",
                "members": ["alice", "bob"],
                "expenses": [
                    {"id": 1, "description": "Tea", "amount": 20.0, "paid_by": "alice", "split": "equal"}
                ],
                "settlements": []
            }
        }
        self._write_store(legacy_store)

        # First migration
        code1, out1, _ = self._run_cli(["migrate-data", "--apply"])
        self.assertEqual(code1, 0)
        first_bytes = self._read_bytes()

        # Second migration (retry)
        code2, out2, _ = self._run_cli(["migrate-data", "--apply"])
        self.assertEqual(code2, 0)
        self.assertIn("Store is already up-to-date", out2)
        second_bytes = self._read_bytes()

        self.assertEqual(first_bytes, second_bytes)

    def test_5_unsupported_future_versions_are_rejected(self):
        """5. Unsupported future versions are rejected with actionable errors and no data modification."""
        future_store = {
            "g1": {
                "schema_version": 999,
                "name": "Future Group",
                "members": ["alice", "bob"],
                "expenses": [],
                "settlements": []
            }
        }
        self._write_store(future_store)
        orig_bytes = self._read_bytes()

        # Try dry-run and apply
        for flag in ["--dry-run", "--apply"]:
            with self.subTest(flag=flag):
                code, out, _ = self._run_cli(["migrate-data", flag])
                self.assertEqual(code, 1)
                self.assertIn("unsupported future schema_version 999", out)
                self.assertIn(f"Current supported maximum version is {CURRENT_SCHEMA_VERSION}", out)
                self.assertEqual(self._read_bytes(), orig_bytes)

        # Also validate-data command rejects future versions
        code, val_out, _ = self._run_cli(["validate-data"])
        self.assertEqual(code, 1)
        self.assertIn("Unsupported future schema_version 999", val_out)

    def test_6_invalid_source_data_blocks_unsafe_migration(self):
        """6. Invalid source data blocks migration before making changes."""
        invalid_store = {
            "g1": {
                "name": "Broken Group",
                "members": ["alice", "bob"],
                "expenses": [
                    # Unknown payer 'charlie'
                    {"id": 1, "description": "Broken", "amount": 50.0, "paid_by": "charlie", "split": "equal"}
                ],
                "settlements": []
            }
        }
        self._write_store(invalid_store)
        orig_bytes = self._read_bytes()

        code, out, _ = self._run_cli(["migrate-data", "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("Migration failed:", out)
        self.assertIn("Pre-migration validation failed", out)
        self.assertIn("Unknown payer 'charlie'", out)
        self.assertEqual(self._read_bytes(), orig_bytes)

    def test_7_multiple_payer_expenses_and_weighted_splits_preserved(self):
        """7. Multiple-payer expenses and weighted splits remain strictly identical after migration."""
        store = {
            "complex_group": {
                "name": "Complex Trip",
                "members": ["alice", "bob", "carol"],
                "expenses": [
                    {
                        "id": 1,
                        "description": "Multi Payer Weighted",
                        "amount": 90.0,
                        "paid_by": {"alice": 60.0, "bob": 30.0},
                        "split": "share",
                        "shares": {"alice": 2, "bob": 1, "carol": 3}
                    }
                ],
                "settlements": []
            }
        }
        self._write_store(store)
        before_balances = get_net_balances(store["complex_group"])
        before_debts = who_owes(store["complex_group"])

        code, out, _ = self._run_cli(["migrate-data", "--apply"])
        self.assertEqual(code, 0)

        migrated_group = self._read_store()["complex_group"]
        self.assertEqual(migrated_group["schema_version"], 1)
        self.assertEqual(migrated_group["expenses"][0]["paid_by"], {"alice": 60.0, "bob": 30.0})
        self.assertEqual(migrated_group["expenses"][0]["split"], "share")
        self.assertEqual(migrated_group["expenses"][0]["shares"], {"alice": 2, "bob": 1, "carol": 3})

        # Calculations match before and after
        after_balances = get_net_balances(migrated_group)
        after_debts = who_owes(migrated_group)
        self.assertEqual(before_balances, after_balances)
        self.assertEqual(before_debts, after_debts)

    def test_8_expense_ids_and_legacy_expenses_without_ids_preserved(self):
        """8. Expense IDs and legacy un-identified expenses are not regenerated or modified."""
        store = {
            "g1": {
                "name": "Mixed IDs",
                "members": ["alice", "bob"],
                "expenses": [
                    {"description": "No ID Expense 1", "amount": 40.0, "paid_by": "alice", "split": "equal"},
                    {"id": 42, "description": "Explicit ID Expense", "amount": 50.0, "paid_by": "bob", "split": "equal"},
                    {"description": "No ID Expense 2", "amount": 60.0, "paid_by": "alice", "split": "equal"},
                ],
                "settlements": []
            }
        }
        self._write_store(store)
        code, out, _ = self._run_cli(["migrate-data", "--apply"])
        self.assertEqual(code, 0)

        migrated_exp = self._read_store()["g1"]["expenses"]
        self.assertNotIn("id", migrated_exp[0])
        self.assertEqual(migrated_exp[1]["id"], 42)
        self.assertNotIn("id", migrated_exp[2])

    def test_9_settlement_history_and_archived_member_metadata_preserved(self):
        """9. Settlement history and optional archived-member metadata remain preserved."""
        store = {
            "g1": {
                "name": "Historical",
                "members": ["alice", "bob", "carol"],
                "archived_members": ["carol"],
                "expenses": [
                    {"id": 1, "description": "Lunch", "amount": 30.0, "paid_by": "alice", "split": "equal"}
                ],
                "settlements": [
                    {"from": "bob", "to": "alice", "amount": 10.0}
                ]
            }
        }
        self._write_store(store)
        code, out, _ = self._run_cli(["migrate-data", "--apply"])
        self.assertEqual(code, 0)

        migrated_group = self._read_store()["g1"]
        self.assertEqual(migrated_group["archived_members"], ["carol"])
        self.assertEqual(migrated_group["settlements"], [{"from": "bob", "to": "alice", "amount": 10.0}])

    def test_10_dry_run_mode_does_not_modify_the_file(self):
        """10. Dry-run mode produces a preview report and leaves the file byte-for-byte unchanged."""
        store = {
            "g1": {
                "name": "Group 1",
                "members": ["alice", "bob"],
                "expenses": [],
                "settlements": []
            },
            "g2": {
                "name": "Group 2",
                "members": ["bob", "charlie"],
                "expenses": [],
                "settlements": []
            }
        }
        self._write_store(store)
        orig_bytes = self._read_bytes()

        code, out, _ = self._run_cli(["migrate-data", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("Dry run: 2 groups proposed for migration:", out)
        self.assertIn("Group 'g1': legacy (unversioned) -> v1", out)
        self.assertIn("Group 'g2': legacy (unversioned) -> v1", out)
        self.assertIn("No changes written to disk. Use '--apply' to execute migration.", out)
        self.assertEqual(self._read_bytes(), orig_bytes)

    def test_11_failed_migration_leaves_original_file_unchanged(self):
        """11. Failed migration (e.g. invalid version or post-validation failure) leaves file unchanged."""
        bad_store = {
            "g1": {
                "schema_version": "not-an-int",
                "name": "Bad Version",
                "members": ["alice"],
                "expenses": [],
                "settlements": []
            }
        }
        self._write_store(bad_store)
        orig_bytes = self._read_bytes()

        code, out, _ = self._run_cli(["migrate-data", "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("Migration failed:", out)
        self.assertIn("has invalid schema_version 'not-an-int'", out)
        self.assertEqual(self._read_bytes(), orig_bytes)

    def test_12_successful_migration_passes_integrity_validator(self):
        """12. Successful migration results in a store that passes validate-data with 0 errors."""
        legacy_store = {
            "groupA": {
                "name": "Alpha",
                "members": ["alice", "bob"],
                "expenses": [
                    {"id": 1, "description": "Snacks", "amount": 25.0, "paid_by": "alice", "split": "equal"}
                ],
                "settlements": []
            }
        }
        self._write_store(legacy_store)

        # Migrate
        code, _, _ = self._run_cli(["migrate-data", "--apply"])
        self.assertEqual(code, 0)

        # Validate
        val_code, val_out, _ = self._run_cli(["validate-data"])
        self.assertEqual(val_code, 0)
        self.assertIn("Validation completed: 1 group checked, 0 errors found.", val_out)

    def test_13_concurrent_writes_are_not_silently_lost(self):
        """13. Concurrent write operations coordinate via database locks and do not lose updates."""
        store = {
            "g1": {
                "name": "Concurrent Group",
                "members": ["alice", "bob"],
                "expenses": [
                    {"id": 1, "description": "Base", "amount": 10.0, "paid_by": "alice", "split": "equal"}
                ],
                "settlements": []
            }
        }
        self._write_store(store)

        def worker_save():
            # simulate concurrent save_group call
            save_group("g2", {
                "schema_version": 1,
                "name": "Other Group",
                "members": ["charlie"],
                "expenses": [],
                "settlements": []
            }, filepath=self.data_path)

        t = threading.Thread(target=worker_save)
        t.start()
        # Concurrently execute migration on g1
        execute_migration(filepath=self.data_path, dry_run=False)
        t.join()

        # Verify both updates persisted
        final_store = self._read_store()
        self.assertIn("g1", final_store)
        self.assertIn("g2", final_store)
        self.assertEqual(final_store["g1"]["schema_version"], 1)
        self.assertEqual(final_store["g2"]["name"], "Other Group")

    def test_14_existing_cli_commands_continue_to_work_with_versioned_groups(self):
        """14. Existing CLI commands (add-expense, edit-expense, delete-expense, settle, settle-plan) work with v1."""
        store = {
            "v1group": {
                "schema_version": 1,
                "name": "Flatmates",
                "members": ["alice", "bob", "charlie"],
                "expenses": [
                    {"id": 1, "description": "Rent", "amount": 300.0, "paid_by": "alice", "split": "equal"}
                ],
                "settlements": []
            }
        }
        self._write_store(store)

        # 1. add-expense
        code, out, _ = self._run_cli(["add-expense", "v1group", "WiFi", "60.0", "bob", "equal"])
        self.assertEqual(code, 0)

        # 2. edit-expense
        code, out, _ = self._run_cli(["edit-expense", "v1group", "2", "High-speed WiFi", "90.0", "bob", "equal"])
        self.assertEqual(code, 0)

        # 3. settle-plan
        code, out, _ = self._run_cli(["settle-plan", "v1group"])
        self.assertEqual(code, 0)
        self.assertIn("must pay", out)

        # 4. settle
        code, out, _ = self._run_cli(["settle", "v1group", "charlie", "alice", "100.0"])
        self.assertEqual(code, 0)

        # 5. delete-expense
        code, out, _ = self._run_cli(["delete-expense", "v1group", "1"])
        self.assertEqual(code, 0)

        # Verify schema_version preserved after all mutations
        final_group = self._read_store()["v1group"]
        self.assertEqual(final_group.get("schema_version"), 1)


if __name__ == "__main__":
    unittest.main()
