"""Metamorphic tests for equivalent weighted-share representations."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from split.calc import (
    _expense_shares_paise,
    get_net_balances,
    settle_plan,
    split_by_share,
    who_owes,
)
from split.cli import main
from split.io import load_group, save_group


class TestEquivalentShareRatios(unittest.TestCase):
    def test_reported_decimal_and_integer_ratios_match(self):
        decimal_result = split_by_share(
            970.60, {"alice": 0.1, "bob": 0.7}
        )
        integer_result = split_by_share(
            970.60, {"alice": 1.0, "bob": 7.0}
        )

        self.assertEqual(decimal_result, integer_result)
        self.assertEqual(decimal_result, {"alice": 121.33, "bob": 849.27})

    def test_scaling_every_weight_preserves_allocations(self):
        cases = [
            (0.01, {"a": 0.1, "b": 0.2, "c": 0.7}),
            (123.45, {"a": 0.25, "b": 0.5, "c": 1.25}),
            (999.99, {"a": 1.2, "b": 3.4, "c": 5.6, "d": 7.8}),
        ]
        for amount, weights in cases:
            with self.subTest(amount=amount, weights=weights):
                expected = split_by_share(amount, weights)
                for factor in (10, 100, 1000):
                    scaled = {
                        person: weight * factor
                        for person, weight in weights.items()
                    }
                    self.assertEqual(split_by_share(amount, scaled), expected)

    def test_equivalent_ratios_produce_identical_debts_and_plans(self):
        def group_with(shares):
            return {
                "members": ["alice", "bob"],
                "expenses": [{
                    "id": 1,
                    "description": "Trip",
                    "amount": 970.60,
                    "paid_by": "bob",
                    "split": "share",
                    "shares": shares,
                }],
                "settlements": [],
            }

        decimal_group = group_with({"alice": 0.1, "bob": 0.7})
        integer_group = group_with({"alice": 1.0, "bob": 7.0})

        self.assertEqual(
            _expense_shares_paise(
                decimal_group["expenses"][0], decimal_group["members"]
            ),
            _expense_shares_paise(
                integer_group["expenses"][0], integer_group["members"]
            ),
        )
        self.assertEqual(
            get_net_balances(decimal_group), get_net_balances(integer_group)
        )
        self.assertEqual(who_owes(decimal_group), who_owes(integer_group))
        self.assertEqual(settle_plan(decimal_group), settle_plan(integer_group))


class TestEquivalentShareRatioCLI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_path = Path(self._tmp.name) / "groups.json"
        base_group = {
            "members": ["alice", "bob"],
            "expenses": [],
            "settlements": [],
        }
        self.data_path.write_text(
            json.dumps({
                "decimal": base_group,
                "integer": {
                    "members": ["alice", "bob"],
                    "expenses": [],
                    "settlements": [],
                },
            }),
            encoding="utf-8",
        )
        self.defaults = (self.data_path,)

    def _run(self, arguments):
        with patch.object(sys, "argv", ["split"] + arguments), \
                patch.object(load_group, "__defaults__", self.defaults), \
                patch.object(save_group, "__defaults__", self.defaults), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main()

    def _assert_group_results_match(self):
        groups = json.loads(self.data_path.read_text(encoding="utf-8"))
        decimal_group = groups["decimal"]
        integer_group = groups["integer"]
        self.assertEqual(
            get_net_balances(decimal_group), get_net_balances(integer_group)
        )
        self.assertEqual(who_owes(decimal_group), who_owes(integer_group))
        self.assertEqual(settle_plan(decimal_group), settle_plan(integer_group))

    def test_add_expense_cli_preserves_equivalent_ratio_results(self):
        self._run([
            "add-expense", "decimal", "Trip", "970.60", "bob", "share",
            "alice=0.1", "bob=0.7",
        ])
        self._run([
            "add-expense", "integer", "Trip", "970.60", "bob", "share",
            "alice=1", "bob=7",
        ])

        self._assert_group_results_match()

    def test_edit_expense_cli_preserves_equivalent_ratio_results(self):
        for group_id in ("decimal", "integer"):
            self._run([
                "add-expense", group_id, "Trip", "970.60", "bob", "equal",
            ])

        self._run([
            "edit-expense", "decimal", "1", "Trip", "970.60", "bob", "share",
            "alice=0.1", "bob=0.7",
        ])
        self._run([
            "edit-expense", "integer", "1", "Trip", "970.60", "bob", "share",
            "alice=1", "bob=7",
        ])

        self._assert_group_results_match()


if __name__ == "__main__":
    unittest.main()
