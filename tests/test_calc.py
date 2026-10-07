"""Unit tests for calculation logic in expense-split."""

import copy
import io
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from split.calc import split_equally, split_by_share, who_owes, get_net_balances, settle_plan
from split.cli import cmd_settle, main


def total_in_paise(result):
    """Adds up split shares exactly, in integer paise."""
    return sum(int(round(value * 100)) for value in result.values())


class TestSplitCalculations(unittest.TestCase):

    def test_split_equally_even_division(self):
        """Even division without remainder should divide accurately."""
        res = split_equally(120, 3)
        self.assertEqual(res["each"], 40)

    def test_split_equally_invalid_people(self):
        """Zero or negative number of people should raise ValueError."""
        with self.assertRaises(ValueError):
            split_equally(100, 0)

    def test_split_by_share_basic(self):
        """Splitting by proportional weights should divide correctly."""
        shares = {"alice": 2, "bob": 1}
        res = split_by_share(90, shares)
        self.assertEqual(res["alice"], 60.0)
        self.assertEqual(res["bob"], 30.0)

    def test_split_by_share_original_bug_totals_exactly(self):
        """₹100 split with equal weights must still total exactly ₹100.00."""
        shares = {"alice": 1, "bob": 1, "charlie": 1}
        res = split_by_share(100, shares)
        self.assertEqual(set(res), {"alice", "bob", "charlie"})
        self.assertEqual(total_in_paise(res), 10000)

    def test_split_by_share_uneven_weights(self):
        """Uneven weights stay proportional and preserve the exact total."""
        shares = {"alice": 2, "bob": 1, "charlie": 1}
        res = split_by_share(100, shares)
        self.assertEqual(set(res), {"alice", "bob", "charlie"})
        self.assertEqual(res["alice"], 50.0)
        self.assertEqual(res["bob"], 25.0)
        self.assertEqual(res["charlie"], 25.0)
        self.assertEqual(total_in_paise(res), 10000)

    def test_split_by_share_rounding_heavy(self):
        """Many fractional paise still leave no money lost or created."""
        shares = {"alice": 1, "bob": 1, "charlie": 1, "dave": 1, "erin": 1}
        res = split_by_share(0.03, shares)
        self.assertEqual(set(res), {"alice", "bob", "charlie", "dave", "erin"})
        self.assertEqual(total_in_paise(res), 3)

    def test_split_by_share_is_deterministic(self):
        """Identical inputs always produce identical output."""
        shares = {"alice": 3, "bob": 2, "charlie": 4}
        first = split_by_share(123.45, shares)
        for _ in range(5):
            self.assertEqual(split_by_share(123.45, shares), first)

    def test_split_by_share_exact_division_unchanged(self):
        """Splitting amounts that divide exactly keeps the expected values."""
        self.assertEqual(split_by_share(90, {"alice": 2, "bob": 1}),
                         {"alice": 60.0, "bob": 30.0})
        self.assertEqual(split_by_share(100, {"alice": 1, "bob": 1}),
                         {"alice": 50.0, "bob": 50.0})

    def test_split_by_share_zero_total_weights(self):
        """Zero or negative total weights should still raise ValueError."""
        with self.assertRaises(ValueError):
            split_by_share(100, {"alice": 0, "bob": 0})
        with self.assertRaises(ValueError):
            split_by_share(100, {"alice": -1, "bob": -1})

    def test_who_owes_empty_group(self):
        """An empty group should return no debts."""
        self.assertEqual(who_owes({}), [])

    def test_balances_sum_to_zero(self):
        """Net balances in paise should always sum to exactly 0."""
        group_data = {
            "members": ["alice", "bob", "charlie"],
            "expenses": [
                {"paid_by": "alice", "amount": 10.00, "split": "equal"},
                {"paid_by": "bob", "amount": 3.33, "split": "equal"}
            ],
            "settlements": [
                {"from": "charlie", "to": "alice", "amount": 2.00}
            ]
        }
        balances = get_net_balances(group_data)
        self.assertEqual(sum(balances.values()), 0)

    def test_no_self_debt(self):
        """Ensure a person cannot owe themselves money."""
        group_data = {
            "members": ["alice", "bob"],
            "expenses": [{"paid_by": "alice", "amount": 100.0, "split": "equal"}]
        }

        # Test who_owes function
        debts = who_owes(group_data)
        for debt in debts:
            self.assertNotEqual(debt["from"], debt["to"], "Bug found: Self-debt detected in who_owes!")

        # Test settle_plan function
        payments = settle_plan(group_data)
        for payment in payments:
            self.assertNotEqual(payment["from"], payment["to"], "Bug found: Self-debt detected in settle_plan!")

class TestAddExpenseCommand(unittest.TestCase):

    def setUp(self):
        self.group_data = {
            "members": ["karan", "siddharth", "arjun"],
            "expenses": [],
            "settlements": []
        }

    def run_command(self, arguments):
        output = io.StringIO()
        with patch.object(sys, "argv", ["split"] + arguments), \
                patch("split.cli.load_group", return_value=self.group_data), \
                patch("split.cli.save_group") as save_group, \
                redirect_stdout(output):
            main()
        return save_group, output.getvalue()

    def test_documented_weighted_share_command(self):
        save_group, output = self.run_command([
            "add-expense", "flatmates", "Groceries", "900", "karan",
            "share", "karan=2", "arjun=1"
        ])

        save_group.assert_called_once_with("flatmates", self.group_data)
        self.assertEqual(
            self.group_data["expenses"][0]["shares"],
            {"karan": 2.0, "arjun": 1.0}
        )
        self.assertEqual(
            who_owes(self.group_data),
            [{"from": "arjun", "to": "karan", "amount": 300.0}]
        )
        self.assertEqual(
            get_net_balances(self.group_data),
            {"karan": 30000, "siddharth": 0, "arjun": -30000}
        )
        self.assertIn("Added expense 'Groceries' of ₹900.00", output)

    def test_malformed_weight_is_rejected(self):
        save_group, output = self.run_command([
            "add-expense", "flatmates", "Groceries", "900", "karan",
            "share", "karan=two", "arjun=1"
        ])

        save_group.assert_not_called()
        self.assertEqual(self.group_data["expenses"], [])
        self.assertIn("Invalid weight 'two' for karan", output)

    def test_equal_expense_still_uses_all_members(self):
        save_group, _ = self.run_command([
            "add-expense", "flatmates", "Dinner", "900", "karan"
        ])

        save_group.assert_called_once_with("flatmates", self.group_data)
        self.assertNotIn("shares", self.group_data["expenses"][0])
        self.assertEqual(
            who_owes(self.group_data),
            [
                {"from": "siddharth", "to": "karan", "amount": 300.0},
                {"from": "arjun", "to": "karan", "amount": 300.0}
            ]
        )


class TestSettleCommand(unittest.TestCase):

    def setUp(self):
        self.group_data = {
            "members": ["ravi", "priya"],
            "expenses": [
                {"paid_by": "priya", "amount": 400.00, "split": "equal"}
            ],
            "settlements": []
        }

    def test_settle_records_payment_in_debt_direction_and_clears_debt(self):
        group = copy.deepcopy(self.group_data)
        args = SimpleNamespace(group="groupA", payer="ravi", receiver="priya")

        with patch("split.cli.load_group", return_value=group), \
                patch("split.cli.save_group") as save_group, \
                redirect_stdout(io.StringIO()):
            cmd_settle(args)

        save_group.assert_called_once_with("groupA", group)
        self.assertEqual(
            group["settlements"],
            [{"from": "ravi", "to": "priya", "amount": 200.0}]
        )
        self.assertEqual(who_owes(group), [])

    def test_settle_rejects_reverse_direction(self):
        group = copy.deepcopy(self.group_data)
        args = SimpleNamespace(group="groupA", payer="priya", receiver="ravi")
        output = io.StringIO()

        with patch("split.cli.load_group", return_value=group), \
                patch("split.cli.save_group") as save_group, \
                redirect_stdout(output):
            cmd_settle(args)

        save_group.assert_not_called()
        self.assertEqual(group["settlements"], [])
        self.assertEqual(
            who_owes(group),
            [{"from": "ravi", "to": "priya", "amount": 200.0}]
        )
        self.assertIn("No pending debt found between priya and ravi.", output.getvalue())


if __name__ == "__main__":
    unittest.main()
