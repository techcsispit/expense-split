"""Regression tests for Issue #18: Inconsistent monetary rounding.

Tests cover:
1. Half-paise rounding boundary values.
2. Decimal values susceptible to binary floating-point representation errors.
3. Equal and weighted expense allocations preserving exact totals in paise.
4. get_net_balances() agreement with expense allocations.
5. who_owes() agreement with net balances.
6. Settlement calculations using the centralized rounding policy.
7. Repeated expenses and settlements preserving monetary conservation.
8. Non-finite and invalid inputs rejection.
9. Backward compatibility for valid inputs.
"""

from decimal import Decimal
import unittest

from split.calc import (
    to_paise,
    split_by_share,
    get_net_balances,
    who_owes,
    settle_plan,
    _expense_shares_paise,
)


class TestMonetaryRounding(unittest.TestCase):

    # 1. Rounding boundaries
    def test_half_paise_rounding_boundaries(self):
        """Values below, at, and above half-paise boundaries round HALF_UP."""
        # Below half-paise boundary (0.49 paise -> 0 paise)
        self.assertEqual(to_paise("0.0049"), 0)
        self.assertEqual(to_paise(0.0049), 0)

        # Exactly at half-paise boundary (0.50 paise -> 1 paise under HALF_UP)
        self.assertEqual(to_paise("0.0050"), 1)
        self.assertEqual(to_paise(0.005), 1)

        # Above half-paise boundary (0.51 paise -> 1 paise)
        self.assertEqual(to_paise("0.0051"), 1)

        # 1.49 paise -> 1 paise
        self.assertEqual(to_paise("0.0149"), 1)

        # 1.50 paise -> 2 paise
        self.assertEqual(to_paise("0.0150"), 2)

        # 1.51 paise -> 2 paise
        self.assertEqual(to_paise("0.0151"), 2)

        # Negative half-paise boundaries
        self.assertEqual(to_paise("-0.0050"), -1)
        self.assertEqual(to_paise("-0.0049"), 0)

    # 2. Binary floating-point representation errors (Regression tests vs old int(round(amount * 100)))
    def test_binary_float_representation_regression_cases(self):
        """Binary float representation errors produce correct rounded paise.

        Old method int(round(amount * 100)) produced incorrect results for:
        - 1.005 * 100 -> 100.49999999999999 -> round() gave 100 instead of 101 paise.
        - 12.345 * 100 -> 1234.5 -> round() (banker's) gave 1234 instead of 1235 paise.
        """
        # 1.005: binary float approximation causes int(round(1.005 * 100)) to give 100
        self.assertEqual(to_paise(1.005), 101)
        self.assertEqual(to_paise("1.005"), 101)
        self.assertEqual(to_paise(Decimal("1.005")), 101)
        self.assertEqual(int(round(1.005 * 100)), 100)
        self.assertNotEqual(int(round(1.005 * 100)), to_paise(1.005))

        # 2.675
        self.assertEqual(to_paise(2.675), 268)
        self.assertEqual(to_paise("2.675"), 268)

        # 12.345: banker's rounding in round() rounds .5 to even (1234), HALF_UP rounds to 1235
        self.assertEqual(to_paise(12.345), 1235)
        self.assertEqual(to_paise("12.345"), 1235)
        self.assertEqual(int(round(12.345 * 100)), 1234)
        self.assertNotEqual(int(round(12.345 * 100)), to_paise(12.345))

    # 3. Preservation of exact total in paise for allocations
    def test_expense_allocations_preserve_exact_total(self):
        """Equal and weighted expense allocations sum to exact total in paise."""
        members = ["alice", "bob", "charlie"]

        # Float amount 2.675 (268 paise) split equally
        exp_equal = {"amount": 2.675, "split": "equal", "paid_by": "alice"}
        shares_equal = _expense_shares_paise(exp_equal, members)
        self.assertEqual(sum(shares_equal.values()), 268)

        # Float amount 1.005 (101 paise) split by share
        exp_share = {
            "amount": 1.005,
            "split": "share",
            "paid_by": "alice",
            "shares": {"alice": 1, "bob": 1, "charlie": 1},
        }
        shares_share = _expense_shares_paise(exp_share, members)
        self.assertEqual(sum(shares_share.values()), 101)

        # Weighted split preserving total
        res = split_by_share(10.05, {"alice": 2, "bob": 1, "charlie": 1})
        res_paise = sum(to_paise(v) for v in res.values())
        self.assertEqual(res_paise, 1005)

    # 4. get_net_balances() agreement
    def test_net_balances_agrees_with_allocations(self):
        """get_net_balances() matches expense allocations and sums to zero."""
        group = {
            "members": ["alice", "bob", "charlie"],
            "expenses": [
                {"paid_by": "alice", "amount": 2.675, "split": "equal"},
            ],
            "settlements": [],
        }

        balances = get_net_balances(group)
        # 268 paise total: alice pays 268, shares are 90, 89, 89
        # alice balance: 268 - 90 = +178 paise
        # bob balance: -89 paise
        # charlie balance: -89 paise
        self.assertEqual(balances["alice"], 178)
        self.assertEqual(balances["bob"], -89)
        self.assertEqual(balances["charlie"], -89)
        self.assertEqual(sum(balances.values()), 0)

    # 5. who_owes() agreement with net balances
    def test_who_owes_agrees_with_net_balances(self):
        """who_owes() debts agree with net balances in paise."""
        group = {
            "members": ["alice", "bob", "charlie"],
            "expenses": [
                {"paid_by": "alice", "amount": 2.675, "split": "equal"},
            ],
            "settlements": [],
        }

        balances = get_net_balances(group)
        debts = who_owes(group)

        # Convert debts back to paise and verify matching net balance
        debt_net = {m: 0 for m in group["members"]}
        for d in debts:
            amount_paise = to_paise(d["amount"])
            debt_net[d["from"]] -= amount_paise
            debt_net[d["to"]] += amount_paise

        self.assertEqual(balances, debt_net)

    # 6. Settlement calculations using same rounding policy
    def test_settlements_use_same_rounding_policy(self):
        """Settlements with float representation errors match rounding policy."""
        group = {
            "members": ["alice", "bob"],
            "expenses": [
                {"paid_by": "alice", "amount": 10.00, "split": "equal"},
            ],
            "settlements": [
                # Bob pays 2.675 (268 paise)
                {"from": "bob", "to": "alice", "amount": 2.675},
            ],
        }

        balances = get_net_balances(group)
        # Alice paid 1000 paise, share 500. Bob share 500.
        # Bob settled 268 paise.
        # Alice balance: 1000 - 500 + 268 = 768 paise.
        # Bob balance: -500 + 268 = -232 paise.
        self.assertEqual(balances["bob"], -232)
        self.assertEqual(balances["alice"], 232)

        debts = who_owes(group)
        self.assertEqual(len(debts), 1)
        self.assertEqual(debts[0]["from"], "bob")
        self.assertEqual(debts[0]["to"], "alice")
        self.assertEqual(to_paise(debts[0]["amount"]), 232)

    # 7. Repeated expenses and settlements money conservation
    def test_money_conservation_over_repeated_operations(self):
        """Repeated expenses and settlements never create or lose money."""
        group = {
            "members": ["alice", "bob", "charlie"],
            "expenses": [],
            "settlements": [],
        }

        amounts = [1.005, 2.675, 12.345, 0.03, 99.99]
        for idx, amt in enumerate(amounts):
            payer = group["members"][idx % len(group["members"])]
            group["expenses"].append(
                {"paid_by": payer, "amount": amt, "split": "equal"}
            )
            # Verify balances always sum to exactly zero in paise
            balances = get_net_balances(group)
            self.assertEqual(sum(balances.values()), 0)

        # Settle pairwise debts returned by who_owes
        while True:
            debts = who_owes(group)
            if not debts:
                break
            d = debts[0]
            group["settlements"].append({"from": d["from"], "to": d["to"], "amount": d["amount"]})

        # After applying settlements to clear debts, all net balances must be exactly zero
        final_balances = get_net_balances(group)
        self.assertTrue(all(b == 0 for b in final_balances.values()))
        self.assertEqual(who_owes(group), [])

    # 8. Non-finite and invalid input rejection
    def test_invalid_and_non_finite_amounts_rejected(self):
        """to_paise rejects NaN, Infinity, non-numeric strings, and None."""
        invalid_inputs = ["nan", "inf", "-inf", float("nan"), float("inf"), float("-inf"), "abc", "12.34.56"]
        for val in invalid_inputs:
            with self.subTest(val=val):
                with self.assertRaises(ValueError):
                    to_paise(val)

        # Unsupported types must raise TypeError
        unsupported = [None, True, False, [100], {"amount": 100}, object(), (1, 2)]
        for val in unsupported:
            with self.subTest(val=val):
                with self.assertRaises(TypeError):
                    to_paise(val)

    # 9. Existing valid monetary inputs
    def test_valid_monetary_inputs_retained(self):
        """Standard valid inputs produce exact integer paise."""
        self.assertEqual(to_paise(100), 10000)
        self.assertEqual(to_paise(50.50), 5050)
        self.assertEqual(to_paise("123.45"), 12345)
        self.assertEqual(to_paise(Decimal("99.99")), 9999)
        self.assertEqual(to_paise(0), 0)

    # 10. Internal integer paise weighted allocation
    def test_split_by_share_paise_returns_int_paise(self):
        """_split_by_share_paise returns integer paise directly."""
        from split.calc import _split_by_share_paise
        result = _split_by_share_paise(10.05, {"alice": 2, "bob": 1, "charlie": 1})
        self.assertTrue(all(isinstance(v, int) for v in result.values()))
        self.assertEqual(result, {"alice": 503, "bob": 251, "charlie": 251})
        self.assertEqual(sum(result.values()), 1005)


if __name__ == "__main__":
    unittest.main()

