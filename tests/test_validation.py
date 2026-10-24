"""Unit tests for Issue #43: Group data integrity validation and diagnostics.

Tests cover:
1. A completely valid group returns exit code 0.
2. Multiple valid groups are all inspected.
3. Missing or malformed top-level data is reported.
4. Missing required group fields are detected.
5. Invalid member structures are detected.
6. Duplicate member names are detected.
7. Unknown expense payer references are detected.
8. Multiple-payer contribution mappings are validated if supported.
9. Contribution totals that do not match the expense amount are detected.
10. Non-finite, zero, negative, or invalid monetary values are detected.
11. Duplicate expense IDs are reported with useful context.
12. Malformed expense IDs are detected.
13. Legacy expenses without IDs remain valid if allowed by the existing schema.
14. Invalid split types are detected.
15. Invalid weighted split references are detected.
16. Invalid weights are detected.
17. Split allocation inconsistencies are detected.
18. Malformed settlement entries are detected.
19. Unknown settlement participants are detected.
20. Invalid settlement amounts are detected.
21. Historical records referencing archived members remain valid when supported by schema.
22. Invalid JSON is reported cleanly without an unhandled traceback from the validation command.
23. A mixture of valid and invalid records produces all practical independent diagnostics.
24. Validation errors produce a non-zero exit code.
25. A valid dataset produces a zero exit code.
26. The store remains unchanged after successful validation.
27. The store remains unchanged after failed validation.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from split.cli import main
from split.validation import validate_store, validate_file, Diagnostic


class TestDataIntegrityValidation(unittest.TestCase):

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

    def _read_bytes(self):
        return self.data_path.read_bytes()

    def _run_validate_cli(self):
        arguments = ["split", "validate-data"]
        out, err = io.StringIO(), io.StringIO()
        exit_code = 0
        with patch.object(sys, "argv", arguments), \
                patch("split.cli.DEFAULT_DATA_PATH", self.data_path), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                main()
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
        return exit_code, out.getvalue(), err.getvalue()

    def test_completely_valid_group_returns_exit_code_zero(self):
        """1 & 25 & 26: A completely valid group returns exit code 0 and leaves file unchanged."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": 1, "description": "Groceries", "amount": 100.0, "paid_by": "alice", "split": "equal"}
            ],
            "settlements": [
                {"from": "bob", "to": "alice", "amount": 50.0}
            ]
        }
        self._write_store({"g1": group})
        before_bytes = self._read_bytes()

        code, out, err = self._run_validate_cli()
        self.assertEqual(code, 0)
        self.assertIn("Validation completed: 1 group checked, 0 errors found.", out)
        self.assertEqual(self._read_bytes(), before_bytes)

    def test_multiple_valid_groups_inspected(self):
        """2: Multiple valid groups are all inspected."""
        groups = {
            "g1": {
                "name": "Group 1",
                "members": ["alice", "bob"],
                "expenses": [],
                "settlements": []
            },
            "g2": {
                "name": "Group 2",
                "members": ["charlie", "dave"],
                "expenses": [
                    {"id": 1, "description": "Taxi", "amount": 40.0, "paid_by": "charlie", "split": "equal"}
                ],
                "settlements": []
            }
        }
        self._write_store(groups)

        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 0)
        self.assertIn("Validation completed: 2 groups checked, 0 errors found.", out)

    def test_missing_or_malformed_top_level_data(self):
        """3: Missing or malformed top-level data is reported."""
        self._write_store(["not", "a", "dict"])
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Top-level data structure must be a JSON object", out)

    def test_missing_required_group_fields(self):
        """4: Missing required group fields are detected."""
        group = {
            "name": "Incomplete Group"
            # Missing members, expenses, settlements
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Missing required 'members' field.", out)
        self.assertIn("Missing required 'expenses' field.", out)
        self.assertIn("Missing required 'settlements' field.", out)

    def test_invalid_member_structures_and_empty_names(self):
        """5: Invalid member structures (non-list, non-strings, empty names) are detected."""
        group = {
            "name": "Bad Members",
            "members": ["alice", 123, "   ", ""],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Member at index 1 must be a non-empty string", out)
        self.assertIn("Member at index 2 must be a non-empty string", out)
        self.assertIn("Member at index 3 must be a non-empty string", out)

    def test_duplicate_member_names_detected(self):
        """6: Duplicate member names are detected."""
        group = {
            "name": "Duplicate Members",
            "members": ["alice", "bob", "Alice"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Duplicate member name 'Alice'.", out)

    def test_unknown_expense_payer_detected(self):
        """7: Unknown expense payer references are detected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": 1, "description": "Dinner", "amount": 50.0, "paid_by": "stranger", "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Unknown payer 'stranger' is not a member of group 'g1'.", out)

    def test_multiple_payer_mapping_valid_and_invalid(self):
        """8 & 9: Multiple-payer contribution mappings validated, and reconciliation errors detected."""
        # Valid multiple-payer
        group_valid = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": 1, "description": "Dinner", "amount": 30.0, "paid_by": {"alice": 20.0, "bob": 10.0}, "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group_valid})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 0)

        # Mismatched multiple-payer
        group_mismatch = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": 1, "description": "Dinner", "amount": 30.0, "paid_by": {"alice": 20.0, "bob": 5.0}, "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group_mismatch})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Contribution total (25.00) does not match expense total (30.00).", out)

        # Unknown contributor in multiple-payer
        group_unknown = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": 1, "description": "Dinner", "amount": 30.0, "paid_by": {"alice": 15.0, "stranger": 15.0}, "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group_unknown})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Unknown contributor 'stranger' is not a member of group 'g1'.", out)

    def test_non_finite_zero_negative_invalid_monetary_amounts(self):
        """10: Non-finite, zero, negative, or unparseable monetary amounts detected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": 1, "description": "E1", "amount": 0, "paid_by": "alice", "split": "equal"},
                {"id": 2, "description": "E2", "amount": -50.0, "paid_by": "alice", "split": "equal"},
                {"id": 3, "description": "E3", "amount": "invalid_amt", "paid_by": "alice", "split": "equal"},
            ],
            "settlements": []
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Expense amount must be greater than 0, got 0", out)
        self.assertIn("Expense amount must be greater than 0, got -50.0", out)
        self.assertIn("Invalid monetary amount 'invalid_amt'", out)

    def test_duplicate_expense_ids_reported(self):
        """11: Duplicate expense IDs are reported with helpful context."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": 5, "description": "First", "amount": 20.0, "paid_by": "alice", "split": "equal"},
                {"id": 5, "description": "Duplicate", "amount": 30.0, "paid_by": "bob", "split": "equal"},
            ],
            "settlements": []
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Duplicate expense ID '5'; also used by expense index 0.", out)

    def test_malformed_expense_ids_detected(self):
        """12: Malformed expense IDs (strings with letters, negatives, floats) are reported without crash."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": "bad-id", "description": "First", "amount": 20.0, "paid_by": "alice", "split": "equal"},
                {"id": -1, "description": "Second", "amount": 20.0, "paid_by": "alice", "split": "equal"},
            ],
            "settlements": []
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Malformed expense ID 'bad-id'.", out)
        self.assertIn("Expense ID must be a positive integer, got -1.", out)

    def test_legacy_expenses_without_ids_remain_valid(self):
        """13: Legacy expenses without IDs remain valid if other fields are valid."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [
                {"description": "Legacy 1", "amount": 40.0, "paid_by": "alice", "split": "equal"},
                {"description": "Legacy 2", "amount": 60.0, "paid_by": "bob", "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 0)
        self.assertIn("Validation completed: 1 group checked, 0 errors found.", out)

    def test_invalid_split_types_detected(self):
        """14: Invalid split types are detected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": 1, "description": "Dinner", "amount": 50.0, "paid_by": "alice", "split": "custom_split"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Invalid split type 'custom_split'.", out)

    def test_invalid_weighted_split_references_and_weights(self):
        """15 & 16: Unknown share participants, non-positive weights, and missing shares detected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [
                # Missing shares
                {"id": 1, "description": "E1", "amount": 50.0, "paid_by": "alice", "split": "share"},
                # Unknown participant in shares
                {"id": 2, "description": "E2", "amount": 50.0, "paid_by": "alice", "split": "share", "shares": {"alice": 1, "stranger": 2}},
                # Negative/zero weight
                {"id": 3, "description": "E3", "amount": 50.0, "paid_by": "alice", "split": "share", "shares": {"alice": 0, "bob": -1}},
                # Non-numeric weight
                {"id": 4, "description": "E4", "amount": 50.0, "paid_by": "alice", "split": "share", "shares": {"alice": "two", "bob": 1}},
            ],
            "settlements": []
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Split type 'share' requires a 'shares' mapping.", out)
        self.assertIn("Unknown participant 'stranger' in shares is not a member of group 'g1'.", out)
        self.assertIn("Weight for 'alice' must be a finite number greater than 0, got 0", out)
        self.assertIn("Weight for 'bob' must be a finite number greater than 0, got -1", out)
        self.assertIn("Invalid non-numeric weight for 'alice': 'two'", out)

    def test_malformed_settlement_entries_detected(self):
        """18 & 19 & 20: Malformed settlements, unknown members, negative amounts, self-settlement."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": [
                # Non-dict
                "not-a-settlement",
                # Missing from/to
                {"amount": 50.0},
                # Unknown member
                {"from": "stranger", "to": "alice", "amount": 50.0},
                # Self-settlement
                {"from": "alice", "to": "alice", "amount": 20.0},
                # Non-positive amount
                {"from": "bob", "to": "alice", "amount": -10.0},
                # Invalid amount
                {"from": "bob", "to": "alice", "amount": "invalid"},
            ]
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Settlement record must be an object", out)
        self.assertIn("Missing required 'from' field.", out)
        self.assertIn("Unknown settlement payer 'stranger' is not a member of group 'g1'.", out)
        self.assertIn("Self-settlement detected: 'alice' cannot settle with themselves.", out)
        self.assertIn("Settlement amount must be positive, got -10.0.", out)
        self.assertIn("Invalid settlement monetary amount 'invalid'", out)

    def test_archived_members_compatibility_when_present(self):
        """21: Archived members in historical records remain valid if listed in members."""
        group = {
            "name": "Trip",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [
                {"id": 1, "description": "Hotel", "amount": 60.0, "paid_by": "carol", "split": "equal"}
            ],
            "settlements": [
                {"from": "bob", "to": "carol", "amount": 20.0}
            ]
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 0)
        self.assertIn("Validation completed: 1 group checked, 0 errors found.", out)

        # But if archived member is not in canonical members, it should be reported
        group_bad_archive = {
            "name": "Trip",
            "members": ["alice", "bob"],
            "archived_members": ["carol"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g2": group_bad_archive})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Archived member 'carol' is not present in 'members'.", out)

    def test_invalid_json_reported_cleanly(self):
        """22: Invalid JSON is reported cleanly without unhandled traceback."""
        self._write_store("{ this is not valid JSON : ")
        code, out, err = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Malformed JSON in", out)
        self.assertNotIn("Traceback", err)

    def test_missing_data_file_reported_cleanly(self):
        """Missing file reports error cleanly."""
        if self.data_path.exists():
            self.data_path.unlink()
        code, out, err = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Data store file not found", out)

    def test_mixture_of_valid_and_invalid_produces_all_independent_diagnostics(self):
        """23: Multiple independent diagnostics are collected rather than failing on first."""
        group = {
            "name": "Mixed Group",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": 1, "description": "Valid", "amount": 20.0, "paid_by": "alice", "split": "equal"},
                {"id": 2, "description": "Bad payer", "amount": 20.0, "paid_by": "ghost", "split": "equal"},
                {"id": 2, "description": "Duplicate ID & bad amt", "amount": -10.0, "paid_by": "bob", "split": "equal"},
            ],
            "settlements": [
                {"from": "alice", "to": "alice", "amount": 10.0}
            ]
        }
        self._write_store({"g1": group})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Unknown payer 'ghost'", out)
        self.assertIn("Duplicate expense ID '2'", out)
        self.assertIn("Expense amount must be greater than 0, got -10.0", out)
        self.assertIn("Self-settlement detected: 'alice' cannot settle with themselves.", out)
        self.assertIn("Validation completed: 1 group checked, 4 errors found.", out)

    def test_store_remains_unchanged_after_failed_validation(self):
        """27: Store file bytes remain unchanged after failed validation."""
        group = {
            "name": "Invalid Group",
            "members": ["alice"],
            "expenses": [
                {"id": 1, "description": "Bad", "amount": -50.0, "paid_by": "alice", "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group})
        before_bytes = self._read_bytes()

        code, _, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertEqual(self._read_bytes(), before_bytes)

    def test_schema_version_validation(self):
        """28: Valid schema_version (1) passes, while non-int and future versions are reported."""
        # Valid v1
        g_v1 = {
            "schema_version": 1,
            "name": "V1 Group",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": g_v1})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 0)
        self.assertIn("Validation completed: 1 group checked, 0 errors found.", out)

        # Invalid type / non-positive
        for bad_ver in ["one", True, False, -1, 0, 1.5]:
            with self.subTest(bad_ver=bad_ver):
                g_bad = {
                    "schema_version": bad_ver,
                    "name": "Bad Ver Group",
                    "members": ["alice"],
                    "expenses": [],
                    "settlements": []
                }
                self._write_store({"g1": g_bad})
                code, out, _ = self._run_validate_cli()
                self.assertEqual(code, 1)
                self.assertIn("Invalid schema_version", out)

        # Future version
        g_future = {
            "schema_version": 2,
            "name": "Future Group",
            "members": ["alice"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": g_future})
        code, out, _ = self._run_validate_cli()
        self.assertEqual(code, 1)
        self.assertIn("Unsupported future schema_version 2", out)


if __name__ == "__main__":
    unittest.main()

