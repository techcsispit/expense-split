"""Export group expense ledgers and settlement summaries to CSV and Markdown statements."""

import csv
import io
from typing import Any, Dict, List
from split.analytics import analyze_categories, detect_category
from split.calc import get_net_balances, settle_plan, to_paise


def export_ledger_csv(group_id: str, group_data: Dict[str, Any]) -> str:
    """Generate a clean, RFC-compliant CSV ledger of all expenses and settlements."""
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Record Type",
        "Index/ID",
        "Description",
        "Category",
        "Amount (INR)",
        "Paid By",
        "Split Type",
        "Date/Details",
    ])

    expenses = group_data.get("expenses", [])
    for idx, exp in enumerate(expenses, 1):
        desc = exp.get("description", f"Expense {idx}")
        cat = detect_category(desc, exp.get("category"))
        amt = exp.get("amount", 0.0)

        paid_by_raw = exp.get("paid_by", "")
        if isinstance(paid_by_raw, dict):
            payers_str = "; ".join(f"{k}: ₹{v}" for k, v in paid_by_raw.items())
        else:
            payers_str = str(paid_by_raw)

        split_type = exp.get("split", "equal")
        date_str = exp.get("date", "")

        writer.writerow([
            "EXPENSE",
            exp.get("id", str(idx)),
            desc,
            cat.capitalize(),
            f"{amt:.2f}",
            payers_str,
            split_type,
            date_str,
        ])

    settlements = group_data.get("settlements", [])
    for idx, stl in enumerate(settlements, 1):
        amt = stl.get("amount", 0.0)
        payer = stl.get("from") or stl.get("payer") or ""
        payee = stl.get("to") or stl.get("payee") or ""
        writer.writerow([
            "SETTLEMENT",
            f"SETTLE-{idx}",
            f"{payer} paid {payee}",
            "Settlement",
            f"{amt:.2f}",
            payer,
            "direct",
            stl.get("date", ""),
        ])

    return output.getvalue()


def export_statement_markdown(group_id: str, group_data: Dict[str, Any]) -> str:
    """Generate a formatted, GitHub-flavored Markdown financial statement."""
    lines = []
    lines.append(f"# Expense Statement for Group: `{group_id}`\n")

    members = group_data.get("members", [])
    lines.append(f"**Members ({len(members)}):** {', '.join(members)}\n")

    # Analytics breakdown
    analytics = analyze_categories(group_data)
    lines.append(f"### Total Group Expenditure: **₹{analytics['total_spent_rupees']:.2f}**\n")

    lines.append("### Category Breakdown")
    lines.append("| Category | Total Spent | % of Budget | Expenses |")
    lines.append("| :--- | :---: | :---: | :---: |")
    for cat_key, info in analytics["breakdown"].items():
        if info["total_paise"] > 0:
            lines.append(
                f"| {info['label']} | ₹{info['total_rupees']:.2f} | {info['percentage']}% | {info['expense_count']} |"
            )
    lines.append("")

    # Net balances
    balances = get_net_balances(group_data)
    lines.append("### Current Member Balances")
    lines.append("| Member | Balance (INR) | Status |")
    lines.append("| :--- | :---: | :--- |")
    for member in members:
        paise = balances.get(member, 0)
        rupees = paise / 100.0
        if paise > 0:
            status = "🟢 Gets Back"
        elif paise < 0:
            status = "🔴 Owes"
        else:
            status = "⚪ Settled"
        lines.append(f"| {member} | ₹{rupees:+.2f} | {status} |")
    lines.append("")

    # Settlement plan
    plan = settle_plan(group_data)
    lines.append("### Optimal Settlement Payments")
    if not plan:
        lines.append("*All debts are settled! No payments required.*\n")
    else:
        lines.append("| Payer | Receiver | Amount |")
        lines.append("| :--- | :--- | :---: |")
        for p in plan:
            lines.append(f"| {p['from']} | {p['to']} | **₹{p['amount']:.2f}** |")
        lines.append("")

    return "\n".join(lines)
