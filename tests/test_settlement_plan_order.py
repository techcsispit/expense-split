"""Regression tests for executing one settlement plan in any order."""

import copy
import io
import itertools
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from split.calc import get_net_balances, settle_plan
from split.cli import (
    cmd_add_expense,
    cmd_delete_expense,
    cmd_edit_expense,
    cmd_settle,
    cmd_settle_plan,
)


def _issue_33_group():
    return {
        "members": ["a", "b", "c", "d", "e"],
        "expenses": [
            {"paid_by": "b", "amount": 5.0, "split": "share", "shares": {"a": 1}},
            {"paid_by": "e", "amount": 14.0, "split": "share", "shares": {"c": 1}},
            {"paid_by": "d", "amount": 3.0, "split": "share", "shares": {"c": 1}},
            {"paid_by": "b", "amount": 1.0, "split": "share", "shares": {"c": 1}},
        ],
        "settlements": [],
    }


class TestSettlementPlanOrder(unittest.TestCase):
    def _settle(self, group, payment, amount=None):
        args = SimpleNamespace(
            group="group",
            payer=payment["from"],
            receiver=payment["to"],
            amount=amount,
        )
        output = io.StringIO()
        with patch("split.cli.load_group", return_value=group), \
                patch("split.cli.save_group") as save_group, \
                redirect_stdout(output):
            cmd_settle(args)
        save_group.assert_called_once_with("group", group)
        return output.getvalue()

    def test_every_payment_order_records_the_original_plan_and_clears_balances(self):
        original = _issue_33_group()
        plan = settle_plan(original)
        self.assertEqual(plan, [
            {"from": "a", "to": "b", "amount": 2.0},
            {"from": "a", "to": "d", "amount": 3.0},
            {"from": "c", "to": "b", "amount": 4.0},
            {"from": "c", "to": "e", "amount": 14.0},
        ])

        expected_records = sorted(
            (payment["from"], payment["to"], payment["amount"])
            for payment in plan
        )
        for order in itertools.permutations(plan):
            with self.subTest(order=order):
                group = copy.deepcopy(original)
                for payment in order:
                    output = self._settle(group, payment)
                    self.assertIn(f"paid ₹{payment['amount']:.2f}", output)

                actual_records = sorted(
                    (payment["from"], payment["to"], payment["amount"])
                    for payment in group["settlements"]
                )
                self.assertEqual(actual_records, expected_records)
                self.assertTrue(all(
                    balance == 0 for balance in get_net_balances(group).values()
                ))
                self.assertNotIn("pending_settlement_plan", group)

    def test_settle_plan_continues_to_show_remaining_saved_payments(self):
        group = _issue_33_group()
        original_plan = settle_plan(group)
        self._settle(group, original_plan[-1])

        output = io.StringIO()
        with patch("split.cli.load_group", return_value=group), redirect_stdout(output):
            cmd_settle_plan(SimpleNamespace(group="group"))

        rendered = output.getvalue()
        for payment in original_plan[:-1]:
            self.assertIn(
                f"{payment['from'].capitalize()} must pay "
                f"{payment['to'].capitalize()} ₹{payment['amount']:.2f}",
                rendered,
            )
        self.assertNotIn("C must pay E", rendered)

    def test_partial_payment_reduces_the_saved_plan_record(self):
        group = _issue_33_group()
        payment = settle_plan(group)[2]

        self._settle(group, payment, amount="1.50")

        saved_record = next(
            item for item in group["pending_settlement_plan"]
            if item["from"] == "c" and item["to"] == "b"
        )
        self.assertEqual(saved_record, {"from": "c", "to": "b", "amount": 2.5})

    def test_invalid_saved_plan_is_ignored(self):
        group = _issue_33_group()
        group["pending_settlement_plan"] = [
            {"from": "a", "to": "e", "amount": 999.0}
        ]
        current_payment = settle_plan(group)[0]

        self._settle(group, current_payment)

        self.assertNotEqual(
            group.get("pending_settlement_plan"),
            [{"from": "a", "to": "e", "amount": 999.0}],
        )

    def test_every_expense_mutation_invalidates_the_saved_plan(self):
        base_group = {
            "members": ["alice", "bob"],
            "expenses": [
                {
                    "id": 1,
                    "description": "Dinner",
                    "amount": 20.0,
                    "paid_by": "alice",
                    "split": "equal",
                },
                {
                    "id": 2,
                    "description": "Taxi",
                    "amount": 10.0,
                    "paid_by": "bob",
                    "split": "equal",
                },
            ],
            "settlements": [],
            "pending_settlement_plan": [
                {"from": "bob", "to": "alice", "amount": 5.0}
            ],
        }
        cases = [
            (
                cmd_add_expense,
                SimpleNamespace(
                    group="group", description="Lunch", amount="12",
                    paid_by="alice", split="equal", share_weights=[],
                ),
            ),
            (
                cmd_edit_expense,
                SimpleNamespace(
                    group="group", expense_id="1", description="Updated",
                    amount="20", paid_by="alice", split="equal",
                    share_weights=[],
                ),
            ),
            (
                cmd_delete_expense,
                SimpleNamespace(group="group", expense_id="2"),
            ),
        ]

        for command, args in cases:
            with self.subTest(command=command.__name__):
                group = copy.deepcopy(base_group)
                with patch("split.cli.load_group", return_value=group), \
                        patch("split.cli.save_group") as save_group, \
                        redirect_stdout(io.StringIO()):
                    command(args)
                save_group.assert_called_once_with("group", group)
                self.assertNotIn("pending_settlement_plan", group)


if __name__ == "__main__":
    unittest.main()
