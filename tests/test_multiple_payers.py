"""Tests for Issue #39: Support expenses paid by multiple participants.

Verifies:
1. Existing single-payer expenses still load and calculate correctly.
2. A multiple-payer expense where contributions equal the total.
3. A participant who contributes and also shares the expense.
4. Equal splits with multiple contributors.
5. Weighted splits with multiple contributors.
6. Contributions with paise-level amounts.
7. Rejection when contribution totals are below the expense total.
8. Rejection when contribution totals exceed the expense total.
9. Rejection of zero, negative, non-finite, and invalid contribution amounts.
10. Rejection of contributors who are not group members.
11. Invalid or empty contributor input.
12. add-expense supports multiple contributors while preserving legacy syntax.
13. edit-expense can update contributors and preserves the expense ID.
14. Failed creation or editing does not change persisted data.
15. get_net_balances() accounts for each contributor correctly.
16. who_owes() produces correct obligations.
17. settle_plan() produces a valid settlement plan with the correct total.
18. Existing partial-settlement behavior remains correct.
19. Existing JSON files with legacy paid_by strings remain usable.
20. Concrete example: Alice ₹20, Bob ₹10, Carol ₹0, sharing ₹30 equally.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from split.calc import get_net_balances, who_owes, settle_plan, to_paise
from split.cli import main
from split.io import load_group, save_group


class TestMultipleExpensePayers(unittest.TestCase):

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.data_path = Path(self._tmp_dir.name) / "groups.json"
        self.defaults = (self.data_path,)

    def _write_store(self, data):
        with open(self.data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _read_store(self):
        with open(self.data_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _run_cli(self, args):
        arguments = ["split"] + args
        out, err = io.StringIO(), io.StringIO()
        handler_returned = True
        with patch.object(sys, "argv", arguments), \
                patch.object(load_group, "__defaults__", self.defaults), \
                patch.object(save_group, "__defaults__", self.defaults), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                main()
            except SystemExit as exc:
                if exc.code == 2:
                    handler_returned = False
                else:
                    raise
        return handler_returned, out.getvalue(), err.getvalue()

    def test_legacy_single_payer_string_loads_and_calculates_correctly(self):
        """1 & 19: Existing JSON files with legacy single-payer paid_by string remain usable."""
        group = {
            "name": "Trip",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": 1, "description": "Hotel", "amount": 100.0, "paid_by": "alice", "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group})

        store = self._read_store()["g1"]
        self.assertEqual(get_net_balances(store), {"alice": 5000, "bob": -5000})
        self.assertEqual(who_owes(store), [{"from": "bob", "to": "alice", "amount": 50.0}])
        plan = settle_plan(store)
        self.assertEqual(plan, [{"from": "bob", "to": "alice", "amount": 50.0}])

    def test_concrete_example_alice_bob_carol_sharing_thirty_equally(self):
        """20: Alice ₹20, Bob ₹10, Carol ₹0 sharing ₹30 equally.

        Each share is ₹10 (1000 paise).
        Alice: paid 2000 - share 1000 = +1000 (owed ₹10)
        Bob: paid 1000 - share 1000 = 0
        Carol: paid 0 - share 1000 = -1000 (owes ₹10)
        Carol owes Alice ₹10.
        """
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "expenses": [
                {
                    "id": 1,
                    "description": "Dinner",
                    "amount": 30.0,
                    "paid_by": {"alice": 20.0, "bob": 10.0},
                    "split": "equal",
                }
            ],
            "settlements": []
        }
        self._write_store({"g1": group})

        store = self._read_store()["g1"]
        balances = get_net_balances(store)
        self.assertEqual(balances, {"alice": 1000, "bob": 0, "carol": -1000})
        debts = who_owes(store)
        self.assertEqual(debts, [{"from": "carol", "to": "alice", "amount": 10.0}])
        plan = settle_plan(store)
        self.assertEqual(plan, [{"from": "carol", "to": "alice", "amount": 10.0}])

    def test_participant_contributes_and_shares_expense(self):
        """3 & 4: Equal splits with multiple contributors, where payers also share expense."""
        group = {
            "name": "Team",
            "members": ["alice", "bob", "carol", "dave"],
            "expenses": [
                {
                    "id": 1,
                    "description": "Team lunch",
                    "amount": 40.0,
                    "paid_by": {"alice": 25.0, "bob": 15.0},
                    "split": "equal",
                }
            ],
            "settlements": []
        }
        # Total 4000 paise, each share is 1000 paise.
        # Alice: paid 2500 - 1000 = +1500
        # Bob: paid 1500 - 1000 = +500
        # Carol: paid 0 - 1000 = -1000
        # Dave: paid 0 - 1000 = -1000
        balances = get_net_balances(group)
        self.assertEqual(balances, {"alice": 1500, "bob": 500, "carol": -1000, "dave": -1000})
        self.assertEqual(sum(balances.values()), 0)

        plan = settle_plan(group)
        # Verify plan clears all balances
        bal_copy = dict(balances)
        for p in plan:
            amt_p = to_paise(p["amount"])
            bal_copy[p["from"]] += amt_p
            bal_copy[p["to"]] -= amt_p
        self.assertTrue(all(v == 0 for v in bal_copy.values()))

    def test_weighted_splits_with_multiple_contributors(self):
        """5: Weighted splits with multiple contributors."""
        group = {
            "name": "Project",
            "members": ["alice", "bob", "carol"],
            "expenses": [
                {
                    "id": 1,
                    "description": "Supplies",
                    "amount": 60.0,
                    "paid_by": {"alice": 40.0, "bob": 20.0},
                    "split": "share",
                    "shares": {"alice": 1, "bob": 2, "carol": 3},
                }
            ],
            "settlements": []
        }
        # Total 6000 paise. Weights: 1 + 2 + 3 = 6 shares.
        # Shares: Alice: 1000, Bob: 2000, Carol: 3000.
        # Paid: Alice 4000, Bob 2000, Carol 0.
        # Net balances:
        # Alice: 4000 - 1000 = +3000
        # Bob: 2000 - 2000 = 0
        # Carol: 0 - 3000 = -3000
        balances = get_net_balances(group)
        self.assertEqual(balances, {"alice": 3000, "bob": 0, "carol": -3000})
        self.assertEqual(who_owes(group), [{"from": "carol", "to": "alice", "amount": 30.0}])

    def test_contributions_with_paise_level_amounts(self):
        """6: Contributions with paise-level fractional amounts."""
        group = {
            "name": "Trip",
            "members": ["alice", "bob"],
            "expenses": [
                {
                    "id": 1,
                    "description": "Taxi",
                    "amount": 10.55,
                    "paid_by": {"alice": 5.30, "bob": 5.25},
                    "split": "equal",
                }
            ],
            "settlements": []
        }
        # Total: 1055 paise.
        # Equal split among 2 people: 1055 // 2 = 527. Remainder 1 -> Alice: 528, Bob: 527.
        # Alice paid: 530. Net: 530 - 528 = +2 paise.
        # Bob paid: 525. Net: 525 - 527 = -2 paise.
        balances = get_net_balances(group)
        self.assertEqual(balances, {"alice": 2, "bob": -2})
        self.assertEqual(who_owes(group), [{"from": "bob", "to": "alice", "amount": 0.02}])

    def test_who_owes_rounding_preserves_each_creditor_net_balance(self):
        """Issue #47: matrix rounding must preserve both sides exactly."""
        group = {
            "name": "Shared purchase",
            "members": ["alice", "bob", "carol", "dave"],
            "expenses": [
                {
                    "id": 1,
                    "description": "Supplies",
                    "amount": 2.02,
                    "paid_by": {"alice": 1.01, "bob": 1.01},
                    "split": "share",
                    "shares": {"carol": 1, "dave": 1},
                }
            ],
            "settlements": [],
        }

        balances = get_net_balances(group)
        debts = who_owes(group)
        implied = {member: 0 for member in group["members"]}
        for debt in debts:
            amount_paise = to_paise(debt["amount"])
            implied[debt["from"]] -= amount_paise
            implied[debt["to"]] += amount_paise

        self.assertEqual(
            balances,
            {"alice": 101, "bob": 101, "carol": -101, "dave": -101},
        )
        self.assertEqual(implied, balances)
        self.assertEqual(debts, [
            {"from": "carol", "to": "alice", "amount": 0.51},
            {"from": "dave", "to": "alice", "amount": 0.50},
            {"from": "carol", "to": "bob", "amount": 0.50},
            {"from": "dave", "to": "bob", "amount": 0.51},
        ])

    def test_add_expense_cli_multiple_payers_equal_split(self):
        """12: add-expense supports multiple contributors while preserving single-payer."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})

        success, out, _ = self._run_cli([
            "add-expense", "g1", "Groceries", "30.0", "alice=20.00,bob=10.00"
        ])
        self.assertTrue(success)
        self.assertIn("Added expense 'Groceries' of ₹30.00 to g1.", out)

        store = self._read_store()["g1"]
        self.assertEqual(len(store["expenses"]), 1)
        exp = store["expenses"][0]
        self.assertEqual(exp["id"], 1)
        self.assertEqual(exp["amount"], 30.0)
        self.assertEqual(exp["paid_by"], {"alice": 20.0, "bob": 10.0})
        self.assertEqual(get_net_balances(store), {"alice": 1000, "bob": 0, "carol": -1000})

    def test_add_expense_cli_multiple_payers_weighted_split(self):
        """add-expense supports multiple payers with weighted share split."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})

        success, out, _ = self._run_cli([
            "add-expense", "g1", "Party", "60.0", "alice=40,bob=20", "share", "alice=1", "bob=2", "carol=3"
        ])
        self.assertTrue(success)
        store = self._read_store()["g1"]
        exp = store["expenses"][0]
        self.assertEqual(exp["split"], "share")
        self.assertEqual(exp["shares"], {"alice": 1.0, "bob": 2.0, "carol": 3.0})
        self.assertEqual(get_net_balances(store), {"alice": 3000, "bob": 0, "carol": -3000})

    def test_edit_expense_cli_switch_single_to_multiple_and_back(self):
        """13: edit-expense can update contributors and preserves expense ID."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "expenses": [
                {"id": 42, "description": "Dinner", "amount": 30.0, "paid_by": "alice", "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group})

        # Edit single payer to multiple payers
        success, out, _ = self._run_cli([
            "edit-expense", "g1", "42", "Dinner", "30.0", "alice=15,bob=15", "equal"
        ])
        self.assertTrue(success)
        self.assertIn("Updated expense ID 42 in g1.", out)

        store = self._read_store()["g1"]
        exp = store["expenses"][0]
        self.assertEqual(exp["id"], 42)
        self.assertEqual(exp["paid_by"], {"alice": 15.0, "bob": 15.0})
        self.assertEqual(get_net_balances(store), {"alice": 500, "bob": 500, "carol": -1000})

        # Edit back to single payer bob
        success, out, _ = self._run_cli([
            "edit-expense", "g1", "42", "Dinner", "30.0", "bob", "equal"
        ])
        self.assertTrue(success)
        store = self._read_store()["g1"]
        exp = store["expenses"][0]
        self.assertEqual(exp["id"], 42)
        self.assertEqual(exp["paid_by"], "bob")
        self.assertEqual(get_net_balances(store), {"alice": -1000, "bob": 2000, "carol": -1000})

    def test_rejection_when_contribution_totals_below_expense_total(self):
        """7: Reject when contribution totals are below expense total."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli([
            "add-expense", "g1", "Dinner", "50.0", "alice=20,bob=20"
        ])
        self.assertTrue(success)
        self.assertIn("Error: Total contributions (₹40.00) must equal the expense total (₹50.00).", out)
        self.assertEqual(self._read_store(), before)

    def test_rejection_when_contribution_totals_exceed_expense_total(self):
        """8: Reject when contribution totals exceed expense total."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli([
            "add-expense", "g1", "Dinner", "50.0", "alice=30,bob=30"
        ])
        self.assertTrue(success)
        self.assertIn("Error: Total contributions (₹60.00) must equal the expense total (₹50.00).", out)
        self.assertEqual(self._read_store(), before)

    def test_rejection_of_zero_negative_nonfinite_contributions(self):
        """9: Reject zero, negative, non-finite, and invalid contribution amounts."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        for invalid_payer in [
            "alice=0,bob=50",
            "alice=-10,bob=60",
            "alice=nan,bob=50",
            "alice=inf,bob=50",
            "alice=abc,bob=50",
        ]:
            with self.subTest(invalid_payer=invalid_payer):
                success, out, _ = self._run_cli([
                    "add-expense", "g1", "Dinner", "50.0", invalid_payer
                ])
                self.assertTrue(success)
                self.assertIn("Error:", out)
                self.assertEqual(self._read_store(), before)

    def test_rejection_of_non_group_member_contributor(self):
        """10: Reject contributors who are not group members."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli([
            "add-expense", "g1", "Dinner", "50.0", "alice=25,stranger=25"
        ])
        self.assertTrue(success)
        self.assertIn("Error: 'stranger' is not a member of the group.", out)
        self.assertEqual(self._read_store(), before)

    def test_rejection_of_duplicate_contributor(self):
        """Duplicate contributors in multiple-payer string must be rejected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli([
            "add-expense", "g1", "Dinner", "50.0", "alice=25,alice=25"
        ])
        self.assertTrue(success)
        self.assertIn("Error: Duplicate contributor 'alice'.", out)
        self.assertEqual(self._read_store(), before)

    def test_rejection_of_malformed_contributor_syntax(self):
        """11: Invalid contributor formats rejected without changing store."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        for bad in ["alice=25,bob", "alice==", "=25", ",", "   "]:
            with self.subTest(bad=bad):
                success, out, _ = self._run_cli([
                    "add-expense", "g1", "Dinner", "50.0", bad
                ])
                self.assertTrue(success)
                self.assertIn("Error:", out)
                self.assertEqual(self._read_store(), before)

    def test_failed_editing_preserves_persisted_data(self):
        """14: Failed editing does not change persisted data."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": 1, "description": "Dinner", "amount": 40.0, "paid_by": "alice", "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        # Attempt invalid edit
        success, out, _ = self._run_cli([
            "edit-expense", "g1", "1", "Dinner", "40.0", "alice=20,bob=10", "equal"
        ])
        self.assertTrue(success)
        self.assertIn("Error:", out)
        self.assertEqual(self._read_store(), before)

    def test_settlements_and_history_preserved_with_multiple_payers(self):
        """18: Existing settlement functionality and partial settlements remain intact."""
        # Alice and Bob pay 40 (Alice 30, Bob 10), split equally between Alice, Bob, Carol.
        # Shares: 13.34, 13.33, 13.33. Total 4000 paise.
        # Alice: 3000 - 1334 = +1666 paise.
        # Bob: 1000 - 1333 = -333 paise.
        # Carol: 0 - 1333 = -1333 paise.
        # Bob settles 3.33 to Alice.
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "expenses": [
                {
                    "id": 1,
                    "description": "Party",
                    "amount": 40.0,
                    "paid_by": {"alice": 30.0, "bob": 10.0},
                    "split": "equal",
                }
            ],
            "settlements": [
                {"from": "bob", "to": "alice", "amount": 3.33}
            ]
        }
        self._write_store({"g1": group})

        store = self._read_store()["g1"]
        balances = get_net_balances(store)
        self.assertEqual(balances["bob"], 0)
        self.assertEqual(balances["alice"], 1333)
        self.assertEqual(balances["carol"], -1333)

        # Now Carol partially settles 5.00 to Alice
        success, out, _ = self._run_cli(["settle", "g1", "carol", "alice", "5.00"])
        self.assertTrue(success)
        self.assertIn("Settled! Carol paid ₹5.00 to Alice.", out)

        store = self._read_store()["g1"]
        self.assertEqual(len(store["settlements"]), 2)
        self.assertEqual(store["settlements"][0], {"from": "bob", "to": "alice", "amount": 3.33})
        self.assertEqual(store["settlements"][1], {"from": "carol", "to": "alice", "amount": 5.0})
        self.assertEqual(get_net_balances(store), {"alice": 833, "bob": 0, "carol": -833})

    def test_summary_displays_multiple_payers(self):
        """summary command outputs multiple payers correctly."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "expenses": [
                {
                    "id": 1,
                    "description": "Lunch",
                    "amount": 30.0,
                    "paid_by": {"alice": 20.0, "bob": 10.0},
                    "split": "equal",
                }
            ],
            "settlements": []
        }
        self._write_store({"g1": group})

        success, out, _ = self._run_cli(["summary", "g1"])
        self.assertTrue(success)
        self.assertIn("paid by alice: ₹20.00, bob: ₹10.00", out)
        self.assertIn("Carol owes Alice ₹10.00", out)

    def test_summary_preserves_multi_payer_creditor_totals_after_rounding(self):
        """The user-facing debt output must agree with calculated balances."""
        group = {
            "name": "Shared purchase",
            "members": ["alice", "bob", "carol", "dave"],
            "expenses": [
                {
                    "id": 1,
                    "description": "Supplies",
                    "amount": 2.02,
                    "paid_by": {"alice": 1.01, "bob": 1.01},
                    "split": "share",
                    "shares": {"carol": 1, "dave": 1},
                }
            ],
            "settlements": [],
        }
        self._write_store({"g1": group})

        success, out, _ = self._run_cli(["summary", "g1"])

        self.assertTrue(success)
        self.assertIn("Carol owes Alice ₹0.51", out)
        self.assertIn("Dave owes Alice ₹0.50", out)
        self.assertIn("Carol owes Bob ₹0.50", out)
        self.assertIn("Dave owes Bob ₹0.51", out)


if __name__ == "__main__":
    unittest.main()

