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

    def test_split_by_share_rejects_zero_individual_weight(self):
        """A zero weight for one participant should raise ValueError."""
        with self.assertRaises(ValueError):
            split_by_share(100, {"alice": 2, "bob": 0})
        with self.assertRaises(ValueError):
            split_by_share(100, {"alice": 0, "bob": 1, "charlie": 1})

    def test_split_by_share_rejects_negative_individual_weight(self):
        """A negative weight for one participant should raise ValueError,
        even when the total weight is positive."""
        with self.assertRaises(ValueError):
            split_by_share(100, {"alice": 3, "bob": -1})
        with self.assertRaises(ValueError):
            split_by_share(100, {"alice": 10, "bob": -2})
        with self.assertRaises(ValueError):
            split_by_share(100, {"alice": -1, "bob": 2})

    def test_split_by_share_rejects_non_positive_mixed_weights(self):
        """Mixed positive/zero/negative weights are all rejected."""
        for shares in ({"alice": 1, "bob": 0, "charlie": 2},
                       {"alice": 5, "bob": -1, "charlie": 1}):
            with self.assertRaises(ValueError):
                split_by_share(100, shares)

    def test_split_by_share_positive_weights_still_work(self):
        """Positive weights, including fractional ones, still split exactly."""
        res = split_by_share(90, {"alice": 0.5, "bob": 0.25, "charlie": 0.25})
        self.assertEqual(set(res), {"alice", "bob", "charlie"})
        self.assertTrue(all(v > 0 for v in res.values()))
        self.assertEqual(total_in_paise(res), 9000)

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


class TestSettlePlan(unittest.TestCase):
    """Tests for minimum-transaction settle_plan calculation."""

    def _verify_plan(self, group_data, expected_max_payments=None):
        """Helper to verify that payments clear all net balances and are valid."""
        initial_balances = get_net_balances(group_data)
        payments = settle_plan(group_data)

        if expected_max_payments is not None:
            self.assertLessEqual(
                len(payments),
                expected_max_payments,
                f"Expected at most {expected_max_payments} payments, got {len(payments)}"
            )

        # Check payment properties
        calculated_balances = dict(initial_balances)
        for p in payments:
            self.assertGreater(p["amount"], 0, "Payment amount must be positive")
            self.assertNotEqual(p["from"], p["to"], "Participant cannot pay themselves")
            amount_paise = int(round(p["amount"] * 100))
            calculated_balances[p["from"]] += amount_paise
            calculated_balances[p["to"]] -= amount_paise

        # Every net balance must be exactly 0 after applying payments
        for person, bal in calculated_balances.items():
            self.assertEqual(bal, 0, f"Net balance for {person} was not cleared: {bal}")

        return payments

    def test_reported_issue_16_example(self):
        """Reported Issue #16 example: 5 participants where greedy gives 4 payments but minimum is 3."""
        group_data = {
            "members": ["alice", "bob", "charlie", "david", "eve"],
            "expenses": [
                {"paid_by": "charlie", "amount": 200.0, "split": "share", "shares": {"alice": 200}},
                {"paid_by": "david", "amount": 200.0, "split": "share", "shares": {"alice": 100, "bob": 100}},
                {"paid_by": "eve", "amount": 100.0, "split": "share", "shares": {"bob": 100}},
            ]
        }
        # Balances: Alice -300, Bob -200, Charlie +200, David +200, Eve +100
        payments = self._verify_plan(group_data, expected_max_payments=3)
        self.assertEqual(len(payments), 3)

    def test_empty_group_or_already_settled(self):
        """Empty group or zero net balances should return no payments."""
        self.assertEqual(settle_plan({}), [])
        self.assertEqual(settle_plan({"members": ["alice", "bob"], "expenses": []}), [])

    def test_single_debtor_single_creditor(self):
        """One debtor and one creditor needs exactly 1 payment."""
        group_data = {
            "members": ["alice", "bob"],
            "expenses": [{"paid_by": "bob", "amount": 100.0, "split": "equal"}]
        }
        payments = self._verify_plan(group_data, expected_max_payments=1)
        self.assertEqual(len(payments), 1)

    def test_participant_with_zero_balance(self):
        """Participants with 0 net balance should not be involved in payments."""
        group_data = {
            "members": ["alice", "bob", "charlie"],
            "expenses": [{"paid_by": "bob", "amount": 100.0, "split": "share", "shares": {"alice": 100}}]
        }
        # Charlie has 0 balance
        payments = self._verify_plan(group_data, expected_max_payments=1)
        for p in payments:
            self.assertNotIn("charlie", (p["from"], p["to"]))

    def test_greedy_already_optimal(self):
        """Cases where simple matching is already optimal."""
        group_data = {
            "members": ["alice", "bob", "charlie"],
            "expenses": [
                {"paid_by": "charlie", "amount": 300.0, "split": "share", "shares": {"alice": 100, "bob": 200}}
            ]
        }
        self._verify_plan(group_data, expected_max_payments=2)

    def test_multiple_zero_sum_subsets(self):
        """Multiple independent zero-sum subsets (e.g., A+B=0, C+D=0)."""
        group_data = {
            "members": ["alice", "bob", "charlie", "david"],
            "expenses": [
                {"paid_by": "bob", "amount": 100.0, "split": "share", "shares": {"alice": 100}},
                {"paid_by": "david", "amount": 200.0, "split": "share", "shares": {"charlie": 200}},
            ]
        }
        # Subsets: {alice: -100, bob: +100} and {charlie: -200, david: +200} -> 2 payments total
        payments = self._verify_plan(group_data, expected_max_payments=2)
        self.assertEqual(len(payments), 2)

    def test_equal_and_unequal_debts(self):
        """Equal and unequal debt combinations maintain exact net balance clearing."""
        group_data = {
            "members": ["a", "b", "c", "d", "e", "f"],
            "expenses": [
                {"paid_by": "a", "amount": 600.0, "split": "equal"}
            ]
        }
        self._verify_plan(group_data)

    def test_brute_force_reference_comparison(self):
        """Compare against brute-force verification for small group configurations."""
        def brute_force_min_transactions(balances_dict):
            """Independent reference solver: finds max zero-sum subset partition size by checking combinations."""
            non_zero_vals = [b for b in balances_dict.values() if b != 0]
            if not non_zero_vals:
                return 0
            n = len(non_zero_vals)

            def max_partitions(elements):
                if not elements:
                    return 0
                first = elements[0]
                rest = elements[1:]
                best_k = -1
                from itertools import combinations
                for r in range(len(rest) + 1):
                    for combo in combinations(rest, r):
                        if first + sum(combo) == 0:
                            rem = list(rest)
                            for item in combo:
                                rem.remove(item)
                            res = max_partitions(rem)
                            if res != -1 and 1 + res > best_k:
                                best_k = 1 + res
                return best_k

            k = max_partitions(non_zero_vals)
            return n - k

        # Test a set of complex balance scenarios
        configs = [
            [-50, -50, 100],
            [-100, -200, 150, 150],
            [-300, -200, 200, 200, 100],
            [-400, -100, 250, 250],
            [-10, -20, -30, 60],
            [-30, -30, -40, 50, 50],
        ]
        for idx, balances_list in enumerate(configs):
            members = [f"m{i}" for i in range(len(balances_list))]
            # Construct expenses where each creditor paid for debtors proportionally
            # To cleanly yield net balances bal_i:
            # For each member i with bal_i > 0, member i pays bal_i for debtor members
            expenses = []
            creditor_indices = [i for i, b in enumerate(balances_list) if b > 0]
            debtor_indices = [i for i, b in enumerate(balances_list) if b < 0]

            # We can create expenses for creditors: creditor c pays c_amount, allocated to debtors in proportion to their debts
            total_debt = sum(-balances_list[d] for d in debtor_indices)
            debt_shares = {members[d]: float(-balances_list[d]) for d in debtor_indices}

            for c in creditor_indices:
                c_amount = float(balances_list[c])
                expenses.append({
                    "paid_by": members[c],
                    "amount": c_amount,
                    "split": "share",
                    "shares": debt_shares
                })

            group_data = {"members": members, "expenses": expenses}
            initial_balances = get_net_balances(group_data)

            # Assert that derived initial_balances exactly equal balances_list in paise
            expected_balances = {members[i]: balances_list[i] * 100 for i in range(len(balances_list))}
            self.assertEqual(
                initial_balances,
                expected_balances,
                f"Config {balances_list}: derived net balances {initial_balances} do not match expected {expected_balances}"
            )

            # 1. Run production settle_plan and verify correctness
            payments = self._verify_plan(group_data)

            # 2. Run independent brute-force solver
            ref_min = brute_force_min_transactions(initial_balances)

            # 3. Assert settle_plan matches minimum transaction count
            self.assertEqual(
                len(payments),
                ref_min,
                f"Config {balances_list}: settle_plan gave {len(payments)} payments, reference min is {ref_min}"
            )


if __name__ == "__main__":
    unittest.main()
