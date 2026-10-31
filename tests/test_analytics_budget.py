import unittest
from split.analytics import detect_category, analyze_categories, STANDARD_CATEGORIES
from split.budget import set_group_budget, clear_group_budget, evaluate_budget_status
from split.export import export_ledger_csv, export_statement_markdown


class TestAnalytics(unittest.TestCase):
    def test_detect_category_explicit_and_inferred(self):
        # Explicit
        self.assertEqual(detect_category("anything", "food"), "food")
        self.assertEqual(detect_category("anything", "Travel"), "travel")
        self.assertEqual(detect_category("anything", "invalid"), "general")

        # Inferred keywords
        self.assertEqual(detect_category("Team dinner at pizzeria"), "food")
        self.assertEqual(detect_category("Uber to airport"), "travel")
        self.assertEqual(detect_category("Hotel room booking"), "stay")
        self.assertEqual(detect_category("Monthly wifi bill"), "utilities")
        self.assertEqual(detect_category("Movie tickets"), "entertainment")
        self.assertEqual(detect_category("Random items bought"), "general")

    def test_analyze_categories_aggregation(self):
        group_data = {
            "members": ["alice", "bob"],
            "expenses": [
                {"description": "Pizza dinner", "amount": 600.0, "paid_by": "alice"},
                {"description": "Cab ride", "amount": 400.0, "paid_by": "bob"},
                {"description": "Groceries", "amount": 200.0, "paid_by": {"alice": 100.0, "bob": 100.0}},
            ],
        }

        result = analyze_categories(group_data)
        self.assertEqual(result["total_spent_rupees"], 1200.0)
        self.assertEqual(result["total_spent_paise"], 120000)
        self.assertEqual(result["top_category"], "food")

        food = result["breakdown"]["food"]
        self.assertEqual(food["total_rupees"], 800.0)
        self.assertEqual(food["expense_count"], 2)
        self.assertEqual(food["percentage"], 66.7)
        self.assertEqual(food["participants"]["alice"], 70000)
        self.assertEqual(food["participants"]["bob"], 10000)

        travel = result["breakdown"]["travel"]
        self.assertEqual(travel["total_rupees"], 400.0)
        self.assertEqual(travel["percentage"], 33.3)

    def test_analyze_categories_empty_group(self):
        result = analyze_categories({"members": ["alice"], "expenses": []})
        self.assertEqual(result["total_spent_rupees"], 0.0)
        self.assertIsNone(result["top_category"])
        for cat in STANDARD_CATEGORIES:
            self.assertEqual(result["breakdown"][cat]["total_paise"], 0)


class TestBudget(unittest.TestCase):
    def test_set_and_clear_budget(self):
        group = {"members": ["alice"]}
        set_group_budget(group, 5000.0)
        self.assertEqual(group["budget_paise"], 500000)

        clear_group_budget(group)
        self.assertNotIn("budget_paise", group)

        with self.assertRaises(ValueError):
            set_group_budget(group, -100)

    def test_budget_status_under_warning_exceeded(self):
        group = {"budget_paise": 100000} # ₹1,000

        # Under (50%)
        st = evaluate_budget_status(group, 50000)
        self.assertEqual(st["status"], "under")
        self.assertEqual(st["usage_percentage"], 50.0)
        self.assertEqual(st["remaining_rupees"], 500.0)

        # Warning (85%)
        st = evaluate_budget_status(group, 85000)
        self.assertEqual(st["status"], "warning")
        self.assertEqual(st["usage_percentage"], 85.0)

        # Exceeded (120%)
        st = evaluate_budget_status(group, 120000)
        self.assertEqual(st["status"], "exceeded")
        self.assertEqual(st["usage_percentage"], 120.0)
        self.assertEqual(st["remaining_rupees"], -200.0)


class TestExport(unittest.TestCase):
    def setUp(self):
        self.group = {
            "name": "Trip to Goa",
            "members": ["alice", "bob"],
            "expenses": [
                {"id": "1", "description": "Resort stay", "amount": 5000.0, "paid_by": "alice", "split": "equal"},
                {"id": "2", "description": "Dinner", "amount": 2000.0, "paid_by": "bob", "split": "equal"},
            ],
            "settlements": [
                {"from": "bob", "to": "alice", "amount": 1000.0, "date": "2026-10-31"},
            ],
        }

    def test_export_ledger_csv(self):
        csv_text = export_ledger_csv("goa", self.group)
        self.assertIn("Record Type,Index/ID,Description,Category,Amount (INR),Paid By,Split Type,Date/Details", csv_text)
        self.assertIn("EXPENSE,1,Resort stay,Stay,5000.00,alice,equal", csv_text)
        self.assertIn("EXPENSE,2,Dinner,Food,2000.00,bob,equal", csv_text)
        self.assertIn("SETTLEMENT,SETTLE-1,bob paid alice,Settlement,1000.00,bob,direct,2026-10-31", csv_text)

    def test_export_statement_markdown(self):
        md_text = export_statement_markdown("goa", self.group)
        self.assertIn("# Expense Statement for Group: `goa`", md_text)
        self.assertIn("Total Group Expenditure:", md_text)
        self.assertIn("### Category Breakdown", md_text)
        self.assertIn("### Current Member Balances", md_text)
        self.assertIn("### Optimal Settlement Payments", md_text)


if __name__ == "__main__":
    unittest.main()
