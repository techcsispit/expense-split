"""Regression tests for Issue #17: add-expense accepts non-member payers.

The add-expense command must reject expenses where paid_by is not a member
of the group, preventing corrupted group data and errors in summary or net
balance calculations.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from split.calc import get_net_balances, who_owes
from split.cli import main
from split.io import load_group, save_group


class TestAddExpensePayerValidation(unittest.TestCase):
    """add-expense must validate that paid_by belongs to the group's members."""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.data_path = Path(self._tmp_dir.name) / "groups.json"
        self.group_data = {
            "name": "Flatmates",
            "members": ["karan", "siddharth", "arjun"],
            "expenses": [],
            "settlements": [],
        }
        self._write_store()

    def _write_store(self):
        with open(self.data_path, "w", encoding="utf-8") as f:
            json.dump({"flatmates": self.group_data}, f, indent=2)

    def _read_store(self):
        with open(self.data_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _run_command(self, args):
        arguments = ["split"] + args
        output, error = io.StringIO(), io.StringIO()
        defaults = (self.data_path,)
        with patch.object(sys, "argv", arguments), \
                patch.object(load_group, "__defaults__", defaults), \
                patch.object(save_group, "__defaults__", defaults), \
                redirect_stdout(output), redirect_stderr(error):
            main()
        return output.getvalue(), error.getvalue()

    def test_valid_member_can_add_expense_successfully(self):
        output, _ = self._run_command(
            ["add-expense", "flatmates", "Groceries", "300", "karan"]
        )
        self.assertIn("Added expense 'Groceries' of ₹300.00 to flatmates.", output)
        store = self._read_store()
        expenses = store["flatmates"]["expenses"]
        self.assertEqual(len(expenses), 1)
        self.assertEqual(expenses[0]["paid_by"], "karan")

    def test_unknown_payer_is_rejected(self):
        before = self._read_store()
        output, _ = self._run_command(
            ["add-expense", "flatmates", "Groceries", "300", "unknown_user"]
        )
        self.assertIn("Error: 'unknown_user' is not a member of the group.", output)
        self.assertEqual(self._read_store(), before)

    def test_rejected_expense_not_persisted_and_data_unchanged(self):
        before_bytes = self.data_path.read_bytes()
        output, _ = self._run_command(
            ["add-expense", "flatmates", "Dinner", "500", "stranger"]
        )
        self.assertIn("Error:", output)
        self.assertEqual(self.data_path.read_bytes(), before_bytes)

    def test_summary_and_net_balance_calculations_work_after_rejection(self):
        # First add a valid expense
        self._run_command(["add-expense", "flatmates", "Lunch", "300", "karan"])
        group_before_rejection = self._read_store()["flatmates"]

        # Attempt to add expense with non-member payer
        self._run_command(["add-expense", "flatmates", "Snacks", "150", "nonmember"])

        group_after_rejection = self._read_store()["flatmates"]
        self.assertEqual(group_before_rejection, group_after_rejection)

        # Verify summary / net balances work without raising KeyError or calculation failures
        balances = get_net_balances(group_after_rejection)
        self.assertEqual(balances, {"karan": 20000, "siddharth": -10000, "arjun": -10000})

        debts = who_owes(group_after_rejection)
        self.assertEqual(len(debts), 2)

        # Also test summary command via CLI
        summary_output, _ = self._run_command(["summary", "flatmates"])
        self.assertIn("Group: Flatmates", summary_output)
        self.assertIn("Total Group Spend: ₹300.00", summary_output)


if __name__ == "__main__":
    unittest.main()

