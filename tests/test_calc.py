"""Unit tests for calculation logic in expense-split."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from split.calc import split_equally, split_by_share, who_owes, get_net_balances
from split.cli import main


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

    def test_split_by_share_keeps_the_total_to_the_paise(self):
        res = split_by_share(1000, {"alice": 1, "bob": 1, "charlie": 1})
        self.assertEqual(sum(res.values()), 1000.0)

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


if __name__ == "__main__":
    unittest.main()
