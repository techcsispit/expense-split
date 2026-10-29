"""Unit tests for Issue #40: Safe group membership management with historical integrity.

Verifies:
1. Existing groups without archived_members load correctly.
2. Adding a valid member succeeds.
3. Duplicate member addition is rejected.
4. Invalid member names are rejected.
5. Archiving an active member succeeds.
6. Archiving an unknown member is rejected.
7. Archiving an already archived member is rejected.
8. Reactivating an archived member succeeds.
9. Reactivating an active or unknown member is rejected.
10. Archived members cannot participate in new expenses (as payer).
11. Archived members cannot participate in new weighted splits.
12. New settlements involving archived members are rejected.
13. Reactivated members can participate in new expenses and settlements.
14. Historical expenses involving archived members remain unchanged and readable.
15. Historical weighted splits referencing archived members remain valid.
16. Historical settlements involving archived members remain unchanged and readable.
17. Outstanding balances involving archived members remain visible and are not silently discarded.
18. Settlement plans handle archived members consistently with documented policy.
19. Invalid membership operations leave persisted group data unchanged.
20. Multiple groups remain isolated when membership is modified.
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
from split.cli import (
    main,
    get_all_members,
    get_active_members,
    get_archived_members,
)
from split.io import load_group, save_group


class TestGroupMembershipLifecycle(unittest.TestCase):

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

    def test_existing_groups_without_archived_members_load_correctly(self):
        """1: Existing groups without archived_members load and treat all members as active."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})

        store = self._read_store()["g1"]
        self.assertEqual(get_all_members(store), ["alice", "bob"])
        self.assertEqual(get_active_members(store), ["alice", "bob"])
        self.assertEqual(get_archived_members(store), [])

        success, out, _ = self._run_cli(["summary", "g1"])
        self.assertTrue(success)
        self.assertIn("Members: alice, bob", out)
        self.assertNotIn("archived", out)

    def test_add_valid_member_succeeds(self):
        """2: Adding a valid member succeeds and persists."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})

        success, out, _ = self._run_cli(["add-member", "g1", "carol"])
        self.assertTrue(success)
        self.assertIn("Added member 'carol' to g1.", out)

        store = self._read_store()["g1"]
        self.assertEqual(get_all_members(store), ["alice", "bob", "carol"])
        self.assertEqual(get_active_members(store), ["alice", "bob", "carol"])

    def test_duplicate_member_addition_rejected(self):
        """3: Duplicate member addition is rejected and does not mutate store."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli(["add-member", "g1", "alice"])
        self.assertTrue(success)
        self.assertIn("Error: 'alice' is already a member of group 'g1'.", out)
        self.assertEqual(self._read_store(), before)

    def test_add_member_when_archived_explains_reactivate(self):
        """If member already exists but is archived, explain reactivate-member should be used."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli(["add-member", "g1", "carol"])
        self.assertTrue(success)
        self.assertIn("Error: 'carol' is an archived member. Use 'reactivate-member' to reactivate them.", out)
        self.assertEqual(self._read_store(), before)

    def test_invalid_member_names_rejected(self):
        """4: Invalid member names (empty, commas, equal signs) are rejected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        for invalid_name in ["", "   ", "carol=10", "carol,dave"]:
            with self.subTest(invalid_name=invalid_name):
                success, out, _ = self._run_cli(["add-member", "g1", invalid_name])
                self.assertTrue(success)
                self.assertIn("Error:", out)
                self.assertEqual(self._read_store(), before)

    def test_archiving_active_member_succeeds(self):
        """5: Archiving an active member succeeds without deleting their identity."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})

        success, out, _ = self._run_cli(["archive-member", "g1", "carol"])
        self.assertTrue(success)
        self.assertIn("Archived member 'carol' in g1.", out)

        store = self._read_store()["g1"]
        self.assertEqual(get_all_members(store), ["alice", "bob", "carol"])
        self.assertEqual(get_active_members(store), ["alice", "bob"])
        self.assertEqual(get_archived_members(store), ["carol"])

    def test_archiving_unknown_member_rejected(self):
        """6: Archiving an unknown member is rejected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli(["archive-member", "g1", "stranger"])
        self.assertTrue(success)
        self.assertIn("Error: 'stranger' is not a member of group 'g1'.", out)
        self.assertEqual(self._read_store(), before)

    def test_archiving_already_archived_member_rejected(self):
        """7: Archiving an already archived member is rejected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli(["archive-member", "g1", "carol"])
        self.assertTrue(success)
        self.assertIn("Error: 'carol' is already archived in group 'g1'.", out)
        self.assertEqual(self._read_store(), before)

    def test_reactivating_archived_member_succeeds(self):
        """8: Reactivating an archived member succeeds and restores active membership."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})

        success, out, _ = self._run_cli(["reactivate-member", "g1", "carol"])
        self.assertTrue(success)
        self.assertIn("Reactivated member 'carol' in g1.", out)

        store = self._read_store()["g1"]
        self.assertEqual(get_all_members(store), ["alice", "bob", "carol"])
        self.assertEqual(get_active_members(store), ["alice", "bob", "carol"])
        self.assertEqual(get_archived_members(store), [])

    def test_reactivating_active_or_unknown_member_rejected(self):
        """9: Reactivating an active or unknown member is rejected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        # Unknown
        success, out, _ = self._run_cli(["reactivate-member", "g1", "stranger"])
        self.assertTrue(success)
        self.assertIn("Error: 'stranger' is not a member of group 'g1'.", out)
        self.assertEqual(self._read_store(), before)

        # Already active
        success, out, _ = self._run_cli(["reactivate-member", "g1", "alice"])
        self.assertTrue(success)
        self.assertIn("Error: 'alice' is already active in group 'g1'.", out)
        self.assertEqual(self._read_store(), before)

    def test_archived_member_cannot_participate_in_new_expenses(self):
        """10: Archived members cannot participate as payer in new expenses."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli(["add-expense", "g1", "Dinner", "30.0", "carol", "equal"])
        self.assertTrue(success)
        self.assertIn("Error: 'carol' is archived. Reactivate the member before adding an expense.", out)
        self.assertEqual(self._read_store(), before)

    def test_archived_member_cannot_participate_in_new_weighted_splits(self):
        """11: Archived members cannot be included in new weighted splits."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli([
            "add-expense", "g1", "Dinner", "30.0", "alice", "share", "alice=1", "carol=1"
        ])
        self.assertTrue(success)
        self.assertIn("Error: 'carol' is archived. Reactivate the member before using them in an expense split.", out)
        self.assertEqual(self._read_store(), before)

    def test_new_settlements_involving_archived_members_rejected(self):
        """12: New settlements involving archived members (as payer or receiver) are rejected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [
                {"id": 1, "description": "Dinner", "amount": 30.0, "paid_by": "alice", "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        # Carol is debtor
        success, out, _ = self._run_cli(["settle", "g1", "carol", "alice"])
        self.assertTrue(success)
        self.assertIn("Error: 'carol' is archived. Reactivate the member before settling debts.", out)
        self.assertEqual(self._read_store(), before)

        # Carol as receiver
        group2 = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [
                {"id": 1, "description": "Dinner", "amount": 30.0, "paid_by": "carol", "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g2": group2})
        before2 = self._read_store()

        success, out, _ = self._run_cli(["settle", "g2", "alice", "carol"])
        self.assertTrue(success)
        self.assertIn("Error: 'carol' is archived. Reactivate the member before settling debts.", out)
        self.assertEqual(self._read_store(), before2)

    def test_reactivated_member_can_participate_in_new_expenses_and_settlements(self):
        """13: Reactivated members can participate in new expenses and settlements."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [
                {"id": 1, "description": "Dinner", "amount": 30.0, "paid_by": "alice", "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group})

        # Reactivate Carol
        success, out, _ = self._run_cli(["reactivate-member", "g1", "carol"])
        self.assertTrue(success)

        # Now Carol can settle
        success, out, _ = self._run_cli(["settle", "g1", "carol", "alice", "10.00"])
        self.assertTrue(success)
        self.assertIn("Settled! Carol paid ₹10.00 to Alice.", out)

        # And Carol can add an expense
        success, out, _ = self._run_cli(["add-expense", "g1", "Snacks", "15.0", "carol", "equal"])
        self.assertTrue(success)
        self.assertIn("Added expense 'Snacks' of ₹15.00 to g1.", out)

    def test_historical_expenses_and_splits_involving_archived_members_remain_valid(self):
        """14, 15, 16: Historical expenses, weighted splits, and settlements involving archived members stay valid."""
        group = {
            "name": "Trip",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [
                {"id": 1, "description": "Hotel", "amount": 90.0, "paid_by": "carol", "split": "equal"},
                {"id": 2, "description": "Dinner", "amount": 60.0, "paid_by": "alice", "split": "share", "shares": {"alice": 1, "bob": 1, "carol": 1}},
            ],
            "settlements": [
                {"from": "bob", "to": "carol", "amount": 10.0}
            ]
        }
        self._write_store({"g1": group})

        store = self._read_store()["g1"]
        # Balances calculation uses all canonical members:
        # Hotel: 9000 paise paid by carol. Each share 3000 paise.
        # Dinner: 6000 paise paid by alice. Each share 2000 paise.
        # Settlement: Bob pays Carol 1000 paise.
        # Net before settlement:
        # Carol: paid 9000 - shares (3000 + 2000) = +4000 paise
        # Alice: paid 6000 - shares (3000 + 2000) = +1000 paise
        # Bob: paid 0 - shares (3000 + 2000) = -5000 paise
        # With settlement:
        # Carol: 4000 - 1000 = 3000
        # Alice: 1000
        # Bob: -5000 + 1000 = -4000
        balances = get_net_balances(store)
        self.assertEqual(balances, {"alice": 1000, "bob": -4000, "carol": 3000})

        # Summary displays debts with archived indicator
        success, out, _ = self._run_cli(["summary", "g1"])
        self.assertTrue(success)
        self.assertIn("Members: alice, bob (archived: carol)", out)
        self.assertIn("Bob owes Carol (archived)", out)
        self.assertIn("Bob owes Alice", out)

    def test_outstanding_balances_and_settle_plan_with_archived_members(self):
        """17 & 18: Outstanding balances and settlement plans include archived members with clear indicator."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [
                {"id": 1, "description": "Groceries", "amount": 30.0, "paid_by": "alice", "split": "equal"}
            ],
            "settlements": []
        }
        self._write_store({"g1": group})

        # Settle plan command
        success, out, _ = self._run_cli(["settle-plan", "g1"])
        self.assertTrue(success)
        self.assertIn("Carol (archived) must pay Alice", out)
        self.assertIn("Bob must pay Alice", out)

    def test_invalid_operations_leave_persisted_group_data_unchanged(self):
        """19: Failed membership commands leave store completely unchanged."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        # Nonexistent group
        self._run_cli(["add-member", "unknown_group", "charlie"])
        self.assertEqual(self._read_store(), before)

        self._run_cli(["archive-member", "unknown_group", "alice"])
        self.assertEqual(self._read_store(), before)

        self._run_cli(["reactivate-member", "unknown_group", "alice"])
        self.assertEqual(self._read_store(), before)

    def test_multiple_contributors_with_active_members_succeeds(self):
        """21: Adding an expense with multiple active contributors succeeds."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})

        success, out, _ = self._run_cli([
            "add-expense", "g1", "Dinner", "30.0", "alice=20.00,bob=10.00", "equal"
        ])
        self.assertTrue(success)
        self.assertIn("Added expense 'Dinner' of ₹30.00 to g1.", out)

        store = self._read_store()["g1"]
        self.assertEqual(len(store["expenses"]), 1)
        self.assertEqual(store["expenses"][0]["paid_by"], {"alice": 20.0, "bob": 10.0})

    def test_multiple_contributors_with_archived_member_rejected(self):
        """22: Adding an expense where one contributor is archived is rejected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli([
            "add-expense", "g1", "Dinner", "30.0", "alice=20.00,carol=10.00", "equal"
        ])
        self.assertTrue(success)
        self.assertIn("Error: 'carol' is archived. Reactivate the member before adding an expense.", out)
        self.assertEqual(self._read_store(), before)

    def test_multiple_contributors_with_unknown_member_rejected(self):
        """23: Adding an expense where one contributor is not a member is rejected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli([
            "add-expense", "g1", "Dinner", "30.0", "alice=20.00,stranger=10.00", "equal"
        ])
        self.assertTrue(success)
        self.assertIn("Error: 'stranger' is not a member of the group.", out)
        self.assertEqual(self._read_store(), before)

    def test_edit_expense_multiple_contributors_with_archived_member_rejected(self):
        """24: Editing an expense to include an archived contributor is rejected."""
        group = {
            "name": "Flatmates",
            "members": ["alice", "bob", "carol"],
            "archived_members": ["carol"],
            "expenses": [
                {
                    "id": 1,
                    "description": "Lunch",
                    "amount": 20.0,
                    "paid_by": "alice",
                    "split": "equal",
                    "shares": None
                }
            ],
            "settlements": []
        }
        self._write_store({"g1": group})
        before = self._read_store()

        success, out, _ = self._run_cli([
            "edit-expense", "g1", "1", "Lunch", "20.0", "alice=10.00,carol=10.00", "equal"
        ])
        self.assertTrue(success)
        self.assertIn("Error: 'carol' is archived. Reactivate the member before adding an expense.", out)
        self.assertEqual(self._read_store(), before)

    def test_multiple_groups_remain_isolated_when_membership_modified(self):
        """20: Modifying membership in one group does not affect other groups."""
        group1 = {
            "name": "Group 1",
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": []
        }
        group2 = {
            "name": "Group 2",
            "members": ["charlie", "dave"],
            "expenses": [],
            "settlements": []
        }
        self._write_store({"g1": group1, "g2": group2})

        self._run_cli(["add-member", "g1", "eve"])
        self._run_cli(["archive-member", "g1", "bob"])

        store = self._read_store()
        self.assertEqual(get_active_members(store["g1"]), ["alice", "eve"])
        self.assertEqual(get_archived_members(store["g1"]), ["bob"])
        # Group 2 completely untouched
        self.assertEqual(store["g2"], group2)


if __name__ == "__main__":
    unittest.main()

