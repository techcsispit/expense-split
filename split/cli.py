"""Command Line Interface for Expense Split."""

import argparse
from decimal import Decimal
import math
import sys
from split.calc import (
    split_equally,
    who_owes,
    settle_plan,
    get_net_balances,
    to_paise,
    _SCALE,
)
from split.io import load_group, save_group, load_all_groups, DEFAULT_DATA_PATH
from split.validation import validate_file
from split.migrations import execute_migration


def cmd_summary(args):
    group = load_group(args.group)
    if not group:
        print(f"Error: Group '{args.group}' not found.")
        return

    print(f"\nGroup: {group.get('name', args.group)}")
    print(f"Members: {', '.join(group.get('members', []))}")
    print("-" * 50)
    print("Expenses:")
    total = 0.0
    for exp in group.get("expenses", []):
        paid_by = exp.get("paid_by")
        if isinstance(paid_by, dict):
            payer_str = ", ".join(f"{p}: ₹{a:.2f}" for p, a in paid_by.items())
            print(f"  • {exp['description']}: ₹{exp['amount']:.2f} (paid by {payer_str})")
        else:
            print(f"  • {exp['description']}: ₹{exp['amount']:.2f} (paid by {paid_by})")
        total += exp["amount"]
    print(f"\nTotal Group Spend: ₹{total:.2f}")

    print("\nDebts & Dues:")
    debts = who_owes(group)
    if not debts:
        print("  All settled up!")
    else:
        for d in debts:
            print(f"  • {d['from'].capitalize()} owes {d['to'].capitalize()} ₹{d['amount']:.2f}")


def _get_max_expense_id(expenses):
    """Return maximum integer id present in expenses, or 0 if none."""
    max_id = 0
    for exp in expenses:
        if isinstance(exp, dict) and "id" in exp:
            try:
                exp_id = int(exp["id"])
                if exp_id > max_id:
                    max_id = exp_id
            except (ValueError, TypeError):
                pass
    return max_id


def _find_expense_index(expenses, target_id):
    """Find index of expense by id (supports int, str, or 1-based index for legacy records).

    First checks explicit 'id' fields. If no expense has matching explicit 'id',
    checks if target_id is a 1-based index matching an un-identified legacy expense.
    Returns None if not found.
    """
    # 1. Exact match by explicit 'id'
    for idx, exp in enumerate(expenses):
        if not isinstance(exp, dict):
            continue
        exp_id = exp.get("id")
        if exp_id is not None and str(exp_id) == str(target_id):
            return idx

    # 2. Backward compatibility fallback: 1-based index for legacy expenses without an 'id'
    try:
        numeric_id = int(target_id)
        if 1 <= numeric_id <= len(expenses):
            candidate = expenses[numeric_id - 1]
            if isinstance(candidate, dict) and "id" not in candidate:
                return numeric_id - 1
    except (ValueError, TypeError):
        pass

    return None


def _plan_clears_current_balances(group, payments):
    """Return whether `payments` exactly clears the group's current balances."""
    if not isinstance(payments, list) or not payments:
        return False

    balances = get_net_balances(group)
    try:
        for payment in payments:
            payer = payment["from"]
            receiver = payment["to"]
            amount_paise = to_paise(payment["amount"])
            if (
                payer == receiver
                or payer not in balances
                or receiver not in balances
                or amount_paise <= 0
            ):
                return False
            balances[payer] += amount_paise
            balances[receiver] -= amount_paise
    except (KeyError, TypeError, ValueError):
        return False

    return all(balance == 0 for balance in balances.values())


def _active_settlement_plan(group):
    """Return a still-valid saved plan, otherwise calculate a fresh plan."""
    pending = group.get("pending_settlement_plan")
    if _plan_clears_current_balances(group, pending):
        return pending
    return settle_plan(group)


def _validate_expense_fields(group, description, amount_raw, paid_by_raw, split_raw, share_weights_raw):
    """Validate common expense fields according to project conventions.

    Supports single payer (e.g. 'alice') or multiple payers specified as
    comma-separated name=amount pairs (e.g. 'alice=20.00,bob=10.00').

    Returns (valid, error_msg, validated_dict) tuple.
    """
    members = group.get("members", [])

    try:
        amount_paise = to_paise(amount_raw)
    except (ValueError, TypeError):
        return False, f"Error: Amount must be a finite number greater than 0, got '{amount_raw}'.", None

    if amount_paise <= 0:
        return False, f"Error: Amount must be a finite number greater than 0, got '{amount_raw}'.", None

    amount = float(Decimal(amount_paise) * _SCALE)

    if not paid_by_raw or not paid_by_raw.strip():
        return False, "Error: Paid by cannot be empty.", None

    paid_by_raw_clean = paid_by_raw.strip()

    if "=" in paid_by_raw_clean or "," in paid_by_raw_clean:
        # Multiple payers syntax: name=amount,name=amount or comma-separated
        payer_entries = [entry.strip() for entry in paid_by_raw_clean.split(",") if entry.strip()]
        if not payer_entries:
            return False, "Error: Paid by cannot be empty.", None

        paid_by_dict = {}
        total_contributed_paise = 0

        for entry in payer_entries:
            if entry.count("=") != 1:
                return False, f"Error: Invalid payer format '{entry}'. Use name=amount.", None

            name, raw_amt = entry.split("=", 1)
            name = name.strip().lower()
            if not name:
                return False, f"Error: Invalid payer format '{entry}'. Use name=amount.", None

            if name not in members:
                return False, f"Error: '{name}' is not a member of the group.", None

            if name in paid_by_dict:
                return False, f"Error: Duplicate contributor '{name}'.", None

            try:
                contrib_paise = to_paise(raw_amt)
            except (ValueError, TypeError):
                return False, f"Error: Contribution for '{name}' must be a finite number greater than 0, got '{raw_amt}'.", None

            if contrib_paise <= 0:
                return False, f"Error: Contribution for '{name}' must be greater than 0, got '{raw_amt}'.", None

            paid_by_dict[name] = float(Decimal(contrib_paise) * _SCALE)
            total_contributed_paise += contrib_paise

        if total_contributed_paise != amount_paise:
            total_contributed_float = float(Decimal(total_contributed_paise) * _SCALE)
            return (
                False,
                f"Error: Total contributions (₹{total_contributed_float:.2f}) must equal the expense total (₹{amount:.2f}).",
                None,
            )

        paid_by = paid_by_dict
    else:
        paid_by = paid_by_raw_clean.lower()
        if paid_by not in members:
            return False, f"Error: '{paid_by}' is not a member of the group.", None

    split = split_raw.lower() if split_raw else "equal"
    if split not in ("equal", "share"):
        return False, f"Error: Invalid split type '{split_raw}'.", None

    shares = None
    if split == "share":
        if not share_weights_raw:
            return False, "Error: Share split requires at least one name=weight value.", None

        shares = {}
        for value in share_weights_raw:
            if value.count("=") != 1:
                return False, f"Error: Invalid share '{value}'. Use name=weight.", None

            name, raw_weight = value.split("=", 1)
            name = name.lower()
            try:
                weight = float(raw_weight)
            except ValueError:
                return False, f"Error: Invalid weight '{raw_weight}' for {name}.", None

            if not name or name not in members:
                return False, f"Error: '{name}' is not a member of the group.", None
            if name in shares:
                return False, f"Error: Duplicate share for '{name}'.", None
            if not math.isfinite(weight) or weight <= 0:
                return False, f"Error: Weight for '{name}' must be more than 0.", None

            shares[name] = weight
    elif share_weights_raw:
        return False, "Error: Share weights can only be used with the 'share' split.", None

    return True, None, {
        "description": description,
        "amount": amount,
        "paid_by": paid_by,
        "split": split,
        "shares": shares,
    }


def cmd_add_expense(args):
    group = load_group(args.group)
    if not group:
        print(f"Error: Group '{args.group}' not found.")
        return

    valid, err_msg, validated = _validate_expense_fields(
        group, args.description, args.amount, args.paid_by, args.split, args.share_weights
    )
    if not valid:
        print(err_msg)
        return

    expenses = group.get("expenses", [])
    next_id = max(len(expenses) + 1, _get_max_expense_id(expenses) + 1)
    new_expense = {
        "id": next_id,
        "description": validated["description"],
        "amount": validated["amount"],
        "paid_by": validated["paid_by"],
        "split": validated["split"]
    }
    if validated["shares"] is not None:
        new_expense["shares"] = validated["shares"]

    expenses.append(new_expense)
    group["expenses"] = expenses
    group.pop("pending_settlement_plan", None)
    save_group(args.group, group)
    print(f"Added expense '{validated['description']}' of ₹{validated['amount']:.2f} to {args.group}.")


def cmd_edit_expense(args):
    group = load_group(args.group)
    if not group:
        print(f"Error: Group '{args.group}' not found.")
        return

    expenses = group.get("expenses", [])
    idx = _find_expense_index(expenses, args.expense_id)
    if idx is None:
        print(f"Error: Expense with ID '{args.expense_id}' not found in group '{args.group}'.")
        return

    valid, err_msg, validated = _validate_expense_fields(
        group, args.description, args.amount, args.paid_by, args.split, args.share_weights
    )
    if not valid:
        print(err_msg)
        return

    target_expense = expenses[idx]
    original_id = target_expense.get("id", args.expense_id)
    # Ensure ID type consistency if original was int
    try:
        original_id = int(original_id)
    except (ValueError, TypeError):
        pass

    updated_expense = {
        "id": original_id,
        "description": validated["description"],
        "amount": validated["amount"],
        "paid_by": validated["paid_by"],
        "split": validated["split"]
    }
    if validated["shares"] is not None:
        updated_expense["shares"] = validated["shares"]

    # In-memory replacement
    new_expenses = list(expenses)
    new_expenses[idx] = updated_expense
    group["expenses"] = new_expenses
    group.pop("pending_settlement_plan", None)
    save_group(args.group, group)
    print(f"Updated expense ID {original_id} in {args.group}.")


def cmd_delete_expense(args):
    group = load_group(args.group)
    if not group:
        print(f"Error: Group '{args.group}' not found.")
        return

    expenses = group.get("expenses", [])
    idx = _find_expense_index(expenses, args.expense_id)
    if idx is None:
        print(f"Error: Expense with ID '{args.expense_id}' not found in group '{args.group}'.")
        return

    target_expense = expenses[idx]
    target_id = target_expense.get("id", args.expense_id)
    target_desc = target_expense.get("description", "")

    new_expenses = [exp for i, exp in enumerate(expenses) if i != idx]
    group["expenses"] = new_expenses
    group.pop("pending_settlement_plan", None)
    save_group(args.group, group)
    if target_desc:
        print(f"Deleted expense ID {target_id} ('{target_desc}') from {args.group}.")
    else:
        print(f"Deleted expense ID {target_id} from {args.group}.")


def cmd_settle(args):
    group = load_group(args.group)
    if not group:
        print(f"Error: Group '{args.group}' not found.")
        return

    payer = args.payer.lower()
    receiver = args.receiver.lower()
    debts = who_owes(group)
    planned_payments = _active_settlement_plan(group)

    # Prefer the globally netted amount when this pair appears in the current
    # plan. The direct pairwise amount can differ, and choosing it would record
    # only part of the payment that settle-plan told the user to make.
    owed_record = next(
        (p for p in planned_payments if p["from"] == payer and p["to"] == receiver),
        None,
    )

    # A direct pairwise debt that is not part of the optimized plan remains a
    # valid settlement too, preserving the existing settle command behavior.
    if not owed_record:
        owed_record = next(
            (d for d in debts if d["from"] == payer and d["to"] == receiver),
            None,
        )
        using_planned_payment = False
    else:
        using_planned_payment = True

    if not owed_record:
        print(f"No pending debt found between {args.payer} and {args.receiver}.")
        return

    max_owed_amount = owed_record["amount"]
    max_owed_paise = to_paise(max_owed_amount)

    raw_amount = getattr(args, "amount", None)
    if raw_amount is not None:
        try:
            payment_paise = to_paise(raw_amount)
        except (ValueError, TypeError):
            print(f"Error: Amount must be a valid number, got '{raw_amount}'.")
            return

        if payment_paise <= 0:
            print(f"Error: Amount must be greater than 0, got '{raw_amount}'.")
            return

        if payment_paise > max_owed_paise:
            print(
                f"Error: Payment amount ₹{float(Decimal(payment_paise) * _SCALE):.2f} "
                f"exceeds outstanding obligation of ₹{max_owed_amount:.2f}."
            )
            return

        amount = float(Decimal(payment_paise) * _SCALE)
    else:
        amount = max_owed_amount

    settlements = group.get("settlements", [])
    settlements.append({
        "from": payer,
        "to": receiver,
        "amount": amount
    })
    group["settlements"] = settlements

    if using_planned_payment:
        remaining_plan = [dict(payment) for payment in planned_payments]
        payment_index = next(
            index
            for index, payment in enumerate(remaining_plan)
            if payment["from"] == payer and payment["to"] == receiver
        )
        remaining_paise = to_paise(remaining_plan[payment_index]["amount"]) - to_paise(amount)
        if remaining_paise == 0:
            remaining_plan.pop(payment_index)
        else:
            remaining_plan[payment_index]["amount"] = float(
                Decimal(remaining_paise) * _SCALE
            )

        if remaining_plan:
            group["pending_settlement_plan"] = remaining_plan
        else:
            group.pop("pending_settlement_plan", None)
    else:
        # A direct pairwise settlement deviates from any saved optimized plan.
        group.pop("pending_settlement_plan", None)

    save_group(args.group, group)
    print(f"Settled! {args.payer.capitalize()} paid ₹{amount:.2f} to {args.receiver.capitalize()}.")


def cmd_settle_plan(args):
    group = load_group(args.group)
    if not group:
        print(f"Error: Group '{args.group}' not found.")
        return

    payments = _active_settlement_plan(group)
    if not payments:
        print("  All settled up!")
    else:
        print("Settle Plan:")
        for p in payments:
            print(f"  • {p['from'].capitalize()} must pay {p['to'].capitalize()} ₹{p['amount']:.2f}")


def cmd_validate_data(args):
    filepath = getattr(args, "filepath", DEFAULT_DATA_PATH)
    diagnostics, stats = validate_file(filepath)

    if diagnostics:
        for diag in diagnostics:
            print(diag.format())
            print()

    groups_count = stats["groups_checked"]
    errors_count = stats["errors_found"]
    group_word = "group" if groups_count == 1 else "groups"
    error_word = "error" if errors_count == 1 else "errors"

    print(f"Validation completed: {groups_count} {group_word} checked, {errors_count} {error_word} found.")

    if errors_count > 0:
        sys.exit(1)


def cmd_migrate_data(args):
    filepath = getattr(args, "filepath", DEFAULT_DATA_PATH)
    dry_run = not getattr(args, "apply", False)

    result = execute_migration(filepath=filepath, dry_run=dry_run)

    if not result["success"]:
        print("Migration failed:")
        for err in result["errors"]:
            print(f"  Error: {err}")
        if result.get("diagnostics"):
            print("\nValidation diagnostics:")
            for diag in result["diagnostics"]:
                print(diag.format())
                print()
        sys.exit(1)

    if result["status"] == "no_op":
        print("Store is already up-to-date. No migrations required.")
        return

    plans = result["plans"]
    count = len(plans)
    group_word = "group" if count == 1 else "groups"

    if dry_run:
        print(f"Dry run: {count} {group_word} proposed for migration:")
        for p in plans:
            src = f"v{p['source_version']}" if p["source_version"] is not None else "legacy (unversioned)"
            tgt = f"v{p['target_version']}"
            print(f"  • Group '{p['group_id']}': {src} -> {tgt}")
        print("\nNo changes written to disk. Use '--apply' to execute migration.")
    else:
        print(f"Successfully migrated {count} {group_word}:")
        for p in plans:
            src = f"v{p['source_version']}" if p["source_version"] is not None else "legacy (unversioned)"
            tgt = f"v{p['target_version']}"
            print(f"  • Group '{p['group_id']}': {src} -> {tgt}")


def main():
    parser = argparse.ArgumentParser(description="Expense Split CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # summary
    p_sum = subparsers.add_parser("summary", help="Show summary and balances for a group")
    p_sum.add_argument("group", help="Group identifier (e.g. groupA, flatmates)")
    p_sum.set_defaults(func=cmd_summary)

    # validate-data
    p_val = subparsers.add_parser("validate-data", help="Inspect and validate group data integrity")
    p_val.set_defaults(func=cmd_validate_data)

    # migrate-data
    p_mig = subparsers.add_parser("migrate-data", help="Migrate group data to current schema version")
    mig_group = p_mig.add_mutually_exclusive_group(required=True)
    mig_group.add_argument("--dry-run", dest="apply", action="store_false", help="Inspect proposed migrations without modifying files (read-only)")
    mig_group.add_argument("--apply", dest="apply", action="store_true", help="Apply migrations and atomically update the store")
    p_mig.set_defaults(func=cmd_migrate_data)

    # add-expense
    p_add = subparsers.add_parser("add-expense", help="Add an expense to a group")
    p_add.add_argument("group", help="Group identifier")
    p_add.add_argument("description", help="Expense description")
    p_add.add_argument("amount", type=float, help="Amount paid")
    p_add.add_argument("paid_by", help="Member who paid, or multiple payers as name=amount,name=amount")
    p_add.add_argument("split", choices=["equal", "share"], default="equal", nargs="?", help="Split type")
    p_add.add_argument("share_weights", nargs="*", help="Share weights as name=weight")
    p_add.set_defaults(func=cmd_add_expense)

    # edit-expense
    p_edit = subparsers.add_parser("edit-expense", help="Edit an existing expense in a group")
    p_edit.add_argument("group", help="Group identifier")
    p_edit.add_argument("expense_id", help="ID of the expense to edit")
    p_edit.add_argument("description", help="New expense description")
    p_edit.add_argument("amount", type=float, help="New amount paid")
    p_edit.add_argument("paid_by", help="Member who paid, or multiple payers as name=amount,name=amount")
    p_edit.add_argument("split", choices=["equal", "share"], default="equal", nargs="?", help="Split type")
    p_edit.add_argument("share_weights", nargs="*", help="Share weights as name=weight")
    p_edit.set_defaults(func=cmd_edit_expense)

    # delete-expense
    p_del = subparsers.add_parser("delete-expense", help="Delete an expense from a group")
    p_del.add_argument("group", help="Group identifier")
    p_del.add_argument("expense_id", help="ID of the expense to delete")
    p_del.set_defaults(func=cmd_delete_expense)

    # settle
    p_set = subparsers.add_parser("settle", help="Settle debts between members")
    p_set.add_argument("group", help="Group identifier")
    p_set.add_argument("payer", help="Person paying the settlement")
    p_set.add_argument("receiver", help="Person receiving the settlement")
    p_set.add_argument("amount", nargs="?", default=None, help="Payment amount (optional, defaults to full settlement)")
    p_set.set_defaults(func=cmd_settle)

    # settle-plan
    p_plan = subparsers.add_parser("settle-plan", help="Show the smallest list of payments to clear everything")
    p_plan.add_argument("group", help="Group identifier")
    p_plan.set_defaults(func=cmd_settle_plan)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
