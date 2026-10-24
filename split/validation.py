"""Data integrity validation for groups, expenses, and settlements."""

from decimal import Decimal
import json
import math
from pathlib import Path
from split.calc import to_paise, _split_by_share_paise
from split.io import DEFAULT_DATA_PATH


class Diagnostic:
    """Represents a data integrity diagnostic."""

    def __init__(self, group, record_type, record_id, field, message, hint=None, severity="ERROR"):
        self.group = group
        self.record_type = record_type
        self.record_id = record_id
        self.field = field
        self.message = message
        self.hint = hint
        self.severity = severity

    def format(self):
        parts = [f"{self.severity}"]
        if self.group is not None:
            parts.append(f"group={self.group}")
        if self.record_type is not None:
            parts.append(f"record={self.record_type}")
        if self.record_id is not None:
            parts.append(f"id={self.record_id}")
        if self.field is not None:
            parts.append(f"field={self.field}")

        header = " ".join(parts)
        res = f"{header}\n  {self.message}"
        if self.hint:
            res += f"\n  Hint: {self.hint}"
        return res

    def __repr__(self):
        return f"<Diagnostic {self.format()}>"


def validate_store(groups_data):
    """Validate loaded groups dictionary.

    Returns: (diagnostics, summary_stats)
    where summary_stats is a dict with counts of groups, expenses, settlements, and errors.
    """
    diagnostics = []
    stats = {
        "groups_checked": 0,
        "expenses_checked": 0,
        "settlements_checked": 0,
        "errors_found": 0,
    }

    if not isinstance(groups_data, dict):
        diagnostics.append(
            Diagnostic(
                group=None,
                record_type="store",
                record_id=None,
                field=None,
                message=f"Top-level data structure must be a JSON object, got {type(groups_data).__name__}.",
                hint="Ensure groups.json contains a top-level mapping of group identifiers to group objects.",
            )
        )
        stats["errors_found"] = len(diagnostics)
        return diagnostics, stats

    for group_id, group in groups_data.items():
        stats["groups_checked"] += 1
        _validate_group(group_id, group, diagnostics, stats)

    stats["errors_found"] = len(diagnostics)
    return diagnostics, stats


def _validate_group(group_id, group, diagnostics, stats):
    if not isinstance(group_id, str) or not group_id.strip():
        diagnostics.append(
            Diagnostic(
                group=str(group_id),
                record_type="group",
                record_id=None,
                field="id",
                message=f"Group identifier must be a non-empty string, got {group_id!r}.",
                hint="Use a non-empty string for the group identifier key.",
            )
        )

    if not isinstance(group, dict):
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="group",
                record_id=None,
                field=None,
                message=f"Group value must be an object, got {type(group).__name__}.",
                hint="Group entry must contain a dictionary with members, expenses, and settlements.",
            )
        )
        return

    # Check schema_version if present
    if "schema_version" in group:
        ver = group["schema_version"]
        if isinstance(ver, bool) or not isinstance(ver, int) or ver <= 0:
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="group",
                    record_id=None,
                    field="schema_version",
                    message=f"Invalid schema_version {ver!r}. Expected a positive integer.",
                    hint="Set schema_version to a positive integer (e.g. 1) or omit for legacy unversioned groups.",
                )
            )
        elif ver > 1:  # Current supported schema version
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="group",
                    record_id=None,
                    field="schema_version",
                    message=f"Unsupported future schema_version {ver}. Highest supported version is 1.",
                    hint="Upgrade expense-split to support newer schema versions.",
                )
            )

    # Check members
    members_raw = group.get("members")
    known_members = set()
    if members_raw is None:
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="group",
                record_id=None,
                field="members",
                message="Missing required 'members' field.",
                hint="Add a 'members' list containing group member names.",
            )
        )
    elif not isinstance(members_raw, list):
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="group",
                record_id=None,
                field="members",
                message=f"'members' must be a list, got {type(members_raw).__name__}.",
                hint="Define members as a list of strings.",
            )
        )
    else:
        seen_members = set()
        for idx, m in enumerate(members_raw):
            if not isinstance(m, str) or not m.strip():
                diagnostics.append(
                    Diagnostic(
                        group=group_id,
                        record_type="member",
                        record_id=idx,
                        field="members",
                        message=f"Member at index {idx} must be a non-empty string, got {m!r}.",
                        hint="Member names must be non-empty strings.",
                    )
                )
            else:
                m_clean = m.strip().lower()
                if m_clean in seen_members:
                    diagnostics.append(
                        Diagnostic(
                            group=group_id,
                            record_type="member",
                            record_id=m,
                            field="members",
                            message=f"Duplicate member name '{m}'.",
                            hint="Ensure each member is listed only once in 'members'.",
                        )
                    )
                else:
                    seen_members.add(m_clean)
                    known_members.add(m_clean)

    # Optional archived_members check (if present in schema/future compatibility)
    if "archived_members" in group:
        archived_raw = group["archived_members"]
        if not isinstance(archived_raw, list):
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="group",
                    record_id=None,
                    field="archived_members",
                    message=f"'archived_members' must be a list, got {type(archived_raw).__name__}.",
                    hint="Define 'archived_members' as a list of member names.",
                )
            )
        else:
            seen_archived = set()
            for idx, am in enumerate(archived_raw):
                if not isinstance(am, str) or not am.strip():
                    diagnostics.append(
                        Diagnostic(
                            group=group_id,
                            record_type="member",
                            record_id=idx,
                            field="archived_members",
                            message=f"Archived member at index {idx} must be a non-empty string, got {am!r}.",
                            hint="Archived member names must be non-empty strings.",
                        )
                    )
                else:
                    am_clean = am.strip().lower()
                    if am_clean in seen_archived:
                        diagnostics.append(
                            Diagnostic(
                                group=group_id,
                                record_type="member",
                                record_id=am,
                                field="archived_members",
                                message=f"Duplicate archived member '{am}'.",
                                hint="List each archived member only once in 'archived_members'.",
                            )
                        )
                    seen_archived.add(am_clean)
                    if am_clean not in known_members:
                        diagnostics.append(
                            Diagnostic(
                                group=group_id,
                                record_type="member",
                                record_id=am,
                                field="archived_members",
                                message=f"Archived member '{am}' is not present in 'members'.",
                                hint="All archived members must also be listed in canonical 'members'.",
                            )
                        )

    # Validate expenses
    expenses_raw = group.get("expenses")
    if expenses_raw is None:
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="group",
                record_id=None,
                field="expenses",
                message="Missing required 'expenses' field.",
                hint="Add an 'expenses' list to the group.",
            )
        )
    elif not isinstance(expenses_raw, list):
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="group",
                record_id=None,
                field="expenses",
                message=f"'expenses' must be a list, got {type(expenses_raw).__name__}.",
                hint="Define expenses as a list of expense objects.",
            )
        )
    else:
        seen_expense_ids = {}
        for idx, exp in enumerate(expenses_raw):
            stats["expenses_checked"] += 1
            _validate_expense(group_id, exp, idx, known_members, seen_expense_ids, diagnostics)

    # Validate settlements
    settlements_raw = group.get("settlements")
    if settlements_raw is None:
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="group",
                record_id=None,
                field="settlements",
                message="Missing required 'settlements' field.",
                hint="Add a 'settlements' list to the group.",
            )
        )
    elif not isinstance(settlements_raw, list):
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="group",
                record_id=None,
                field="settlements",
                message=f"'settlements' must be a list, got {type(settlements_raw).__name__}.",
                hint="Define settlements as a list of settlement objects.",
            )
        )
    else:
        for idx, stl in enumerate(settlements_raw):
            stats["settlements_checked"] += 1
            _validate_settlement(group_id, stl, idx, known_members, diagnostics)


def _validate_expense(group_id, exp, idx, known_members, seen_expense_ids, diagnostics):
    record_id_label = f"[{idx}]"
    if not isinstance(exp, dict):
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="expense",
                record_id=record_id_label,
                field=None,
                message=f"Expense record must be an object, got {type(exp).__name__}.",
                hint="Ensure each expense is a dictionary with id, description, amount, paid_by, and split.",
            )
        )
        return

    exp_id = exp.get("id")
    if exp_id is not None:
        record_id_label = str(exp_id)
        if isinstance(exp_id, bool) or not isinstance(exp_id, (int, str)):
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="expense",
                    record_id=str(exp_id),
                    field="id",
                    message=f"Invalid expense ID type '{type(exp_id).__name__}'.",
                    hint="Expense IDs should be positive integers or integer strings.",
                )
            )
        else:
            try:
                int_id = int(exp_id)
                if int_id <= 0:
                    diagnostics.append(
                        Diagnostic(
                            group=group_id,
                            record_type="expense",
                            record_id=str(exp_id),
                            field="id",
                            message=f"Expense ID must be a positive integer, got {exp_id}.",
                            hint="Use positive integer IDs.",
                        )
                    )
                else:
                    if int_id in seen_expense_ids:
                        prior_idx = seen_expense_ids[int_id]
                        diagnostics.append(
                            Diagnostic(
                                group=group_id,
                                record_type="expense",
                                record_id=str(exp_id),
                                field="id",
                                message=f"Duplicate expense ID '{exp_id}'; also used by expense index {prior_idx}.",
                                hint="Assign a unique ID to each expense.",
                            )
                        )
                    else:
                        seen_expense_ids[int_id] = idx
            except (ValueError, TypeError):
                diagnostics.append(
                    Diagnostic(
                        group=group_id,
                        record_type="expense",
                        record_id=str(exp_id),
                        field="id",
                        message=f"Malformed expense ID '{exp_id}'.",
                        hint="Expense IDs should be integer values.",
                    )
                )

    # description
    desc = exp.get("description")
    if desc is None:
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="expense",
                record_id=record_id_label,
                field="description",
                message="Missing required 'description' field.",
                hint="Provide a text description for the expense.",
            )
        )
    elif not isinstance(desc, str):
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="expense",
                record_id=record_id_label,
                field="description",
                message=f"'description' must be a string, got {type(desc).__name__}.",
                hint="Provide a string description.",
            )
        )

    # amount
    amount_raw = exp.get("amount")
    amount_paise = None
    if amount_raw is None:
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="expense",
                record_id=record_id_label,
                field="amount",
                message="Missing required 'amount' field.",
                hint="Provide a positive monetary amount.",
            )
        )
    else:
        try:
            amount_paise = to_paise(amount_raw)
            if amount_paise <= 0:
                diagnostics.append(
                    Diagnostic(
                        group=group_id,
                        record_type="expense",
                        record_id=record_id_label,
                        field="amount",
                        message=f"Expense amount must be greater than 0, got {amount_raw!r}.",
                        hint="Specify a positive finite amount.",
                    )
                )
                amount_paise = None
        except (ValueError, TypeError) as exc:
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="expense",
                    record_id=record_id_label,
                    field="amount",
                    message=f"Invalid monetary amount {amount_raw!r}: {exc}",
                    hint="Ensure the amount is a finite, positive number.",
                )
            )

    # paid_by
    paid_by = exp.get("paid_by")
    if paid_by is None:
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="expense",
                record_id=record_id_label,
                field="paid_by",
                message="Missing required 'paid_by' field.",
                hint="Specify the member who paid the expense, or a contributors mapping.",
            )
        )
    elif isinstance(paid_by, str):
        payer_clean = paid_by.strip().lower()
        if not payer_clean:
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="expense",
                    record_id=record_id_label,
                    field="paid_by",
                    message="Payer name cannot be empty.",
                    hint="Specify a valid group member name.",
                )
            )
        elif payer_clean not in known_members:
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="expense",
                    record_id=record_id_label,
                    field="paid_by",
                    message=f"Unknown payer '{paid_by}' is not a member of group '{group_id}'.",
                    hint="Payer must be one of the group's members.",
                )
            )
    elif isinstance(paid_by, dict):
        # Multiple-contributor support
        if not paid_by:
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="expense",
                    record_id=record_id_label,
                    field="paid_by",
                    message="Multiple-payer mapping cannot be empty.",
                    hint="Provide at least one contributor with their contributed amount.",
                )
            )
        else:
            total_contributed_paise = 0
            has_contrib_error = False
            seen_contributors = set()

            for contributor, contrib_amt in paid_by.items():
                if not isinstance(contributor, str) or not contributor.strip():
                    diagnostics.append(
                        Diagnostic(
                            group=group_id,
                            record_type="expense",
                            record_id=record_id_label,
                            field="paid_by",
                            message=f"Invalid contributor identifier {contributor!r}.",
                            hint="Contributor names must be non-empty strings.",
                        )
                    )
                    has_contrib_error = True
                    continue

                contrib_clean = contributor.strip().lower()
                if contrib_clean in seen_contributors:
                    diagnostics.append(
                        Diagnostic(
                            group=group_id,
                            record_type="expense",
                            record_id=record_id_label,
                            field="paid_by",
                            message=f"Duplicate contributor '{contributor}'.",
                            hint="Each contributor should appear once in the paid_by mapping.",
                        )
                    )
                    has_contrib_error = True
                seen_contributors.add(contrib_clean)

                if contrib_clean not in known_members:
                    diagnostics.append(
                        Diagnostic(
                            group=group_id,
                            record_type="expense",
                            record_id=record_id_label,
                            field="paid_by",
                            message=f"Unknown contributor '{contributor}' is not a member of group '{group_id}'.",
                            hint="All contributors must be group members.",
                        )
                    )
                    has_contrib_error = True

                try:
                    c_paise = to_paise(contrib_amt)
                    if c_paise <= 0:
                        diagnostics.append(
                            Diagnostic(
                                group=group_id,
                                record_type="expense",
                                record_id=record_id_label,
                                field="paid_by",
                                message=f"Contribution for '{contributor}' must be positive, got {contrib_amt!r}.",
                                hint="Each contribution must be greater than 0.",
                            )
                        )
                        has_contrib_error = True
                    else:
                        total_contributed_paise += c_paise
                except (ValueError, TypeError) as exc:
                    diagnostics.append(
                        Diagnostic(
                            group=group_id,
                            record_type="expense",
                            record_id=record_id_label,
                            field="paid_by",
                            message=f"Invalid contribution amount for '{contributor}': {contrib_amt!r} ({exc}).",
                            hint="Contributions must be valid monetary amounts.",
                        )
                    )
                    has_contrib_error = True

            if amount_paise is not None and not has_contrib_error:
                if total_contributed_paise != amount_paise:
                    diagnostics.append(
                        Diagnostic(
                            group=group_id,
                            record_type="expense",
                            record_id=record_id_label,
                            field="amount",
                            message=(
                                f"Contribution total ({total_contributed_paise / 100:.2f}) "
                                f"does not match expense total ({amount_paise / 100:.2f})."
                            ),
                            hint="The sum of all payer contributions must equal the total expense amount in paise.",
                        )
                    )
    else:
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="expense",
                record_id=record_id_label,
                field="paid_by",
                message=f"Invalid 'paid_by' type {type(paid_by).__name__}.",
                hint="'paid_by' must be a member name string or a mapping of member names to amounts.",
            )
        )

    # split & shares
    split_type = exp.get("split", "equal")
    if not isinstance(split_type, str) or split_type.lower() not in ("equal", "share"):
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="expense",
                record_id=record_id_label,
                field="split",
                message=f"Invalid split type '{split_type}'.",
                hint="Supported split types are 'equal' and 'share'.",
            )
        )
    elif split_type.lower() == "share":
        shares = exp.get("shares")
        if shares is None:
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="expense",
                    record_id=record_id_label,
                    field="shares",
                    message="Split type 'share' requires a 'shares' mapping.",
                    hint="Provide a mapping of participant names to positive share weights.",
                )
            )
        elif not isinstance(shares, dict) or not shares:
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="expense",
                    record_id=record_id_label,
                    field="shares",
                    message=f"'shares' must be a non-empty dictionary, got {shares!r}.",
                    hint="Provide at least one participant with a positive weight.",
                )
            )
        else:
            total_weight = 0
            has_weight_error = False
            for person, weight in shares.items():
                if not isinstance(person, str) or not person.strip():
                    diagnostics.append(
                        Diagnostic(
                            group=group_id,
                            record_type="expense",
                            record_id=record_id_label,
                            field="shares",
                            message=f"Invalid participant identifier in shares: {person!r}.",
                            hint="Participant names must be non-empty strings.",
                        )
                    )
                    has_weight_error = True
                    continue

                p_clean = person.strip().lower()
                if p_clean not in known_members:
                    diagnostics.append(
                        Diagnostic(
                            group=group_id,
                            record_type="expense",
                            record_id=record_id_label,
                            field="shares",
                            message=f"Unknown participant '{person}' in shares is not a member of group '{group_id}'.",
                            hint="All weighted split participants must be group members.",
                        )
                    )
                    has_weight_error = True

                try:
                    w = float(weight)
                    if not math.isfinite(w) or w <= 0:
                        diagnostics.append(
                            Diagnostic(
                                group=group_id,
                                record_type="expense",
                                record_id=record_id_label,
                                field="shares",
                                message=f"Weight for '{person}' must be a finite number greater than 0, got {weight!r}.",
                                hint="Specify positive numeric weights.",
                            )
                        )
                        has_weight_error = True
                    else:
                        total_weight += w
                except (ValueError, TypeError):
                    diagnostics.append(
                        Diagnostic(
                            group=group_id,
                            record_type="expense",
                            record_id=record_id_label,
                            field="shares",
                            message=f"Invalid non-numeric weight for '{person}': {weight!r}.",
                            hint="Weights must be numeric values.",
                        )
                    )
                    has_weight_error = True

            if amount_paise is not None and not has_weight_error:
                # Test allocation calculation
                try:
                    allocations = _split_by_share_paise(amount_paise / 100.0, shares)
                    if sum(allocations.values()) != amount_paise:
                        diagnostics.append(
                            Diagnostic(
                                group=group_id,
                                record_type="expense",
                                record_id=record_id_label,
                                field="shares",
                                message="Calculated share allocations do not sum to total expense amount.",
                                hint="Check share weights and amount.",
                            )
                        )
                except Exception as exc:
                    diagnostics.append(
                        Diagnostic(
                            group=group_id,
                            record_type="expense",
                            record_id=record_id_label,
                            field="shares",
                            message=f"Failed to calculate share split: {exc}",
                            hint="Ensure share weights are valid numbers greater than 0.",
                        )
                    )
    else:
        # equal split
        if "shares" in exp and exp["shares"] is not None:
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="expense",
                    record_id=record_id_label,
                    field="shares",
                    message="'shares' field is present on an 'equal' split expense.",
                    hint="Omit 'shares' or set split to 'share'.",
                )
            )


def _validate_settlement(group_id, stl, idx, known_members, diagnostics):
    record_id_label = f"[{idx}]"
    if not isinstance(stl, dict):
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="settlement",
                record_id=record_id_label,
                field=None,
                message=f"Settlement record must be an object, got {type(stl).__name__}.",
                hint="Ensure each settlement is a dictionary with 'from', 'to', and 'amount'.",
            )
        )
        return

    payer = stl.get("from")
    receiver = stl.get("to")
    amount_raw = stl.get("amount")

    payer_clean = None
    if payer is None:
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="settlement",
                record_id=record_id_label,
                field="from",
                message="Missing required 'from' field.",
                hint="Specify the member paying the settlement.",
            )
        )
    elif not isinstance(payer, str) or not payer.strip():
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="settlement",
                record_id=record_id_label,
                field="from",
                message=f"Invalid 'from' payer name {payer!r}.",
                hint="Payer must be a non-empty string.",
            )
        )
    else:
        payer_clean = payer.strip().lower()
        if payer_clean not in known_members:
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="settlement",
                    record_id=record_id_label,
                    field="from",
                    message=f"Unknown settlement payer '{payer}' is not a member of group '{group_id}'.",
                    hint="Settlement payer must be a member of the group.",
                )
            )

    receiver_clean = None
    if receiver is None:
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="settlement",
                record_id=record_id_label,
                field="to",
                message="Missing required 'to' field.",
                hint="Specify the member receiving the settlement.",
            )
        )
    elif not isinstance(receiver, str) or not receiver.strip():
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="settlement",
                record_id=record_id_label,
                field="to",
                message=f"Invalid 'to' receiver name {receiver!r}.",
                hint="Receiver must be a non-empty string.",
            )
        )
    else:
        receiver_clean = receiver.strip().lower()
        if receiver_clean not in known_members:
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="settlement",
                    record_id=record_id_label,
                    field="to",
                    message=f"Unknown settlement receiver '{receiver}' is not a member of group '{group_id}'.",
                    hint="Settlement receiver must be a member of the group.",
                )
            )

    if payer_clean and receiver_clean and payer_clean == receiver_clean:
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="settlement",
                record_id=record_id_label,
                field="to",
                message=f"Self-settlement detected: '{payer}' cannot settle with themselves.",
                hint="A settlement payer and receiver must be different members.",
            )
        )

    if amount_raw is None:
        diagnostics.append(
            Diagnostic(
                group=group_id,
                record_type="settlement",
                record_id=record_id_label,
                field="amount",
                message="Missing required 'amount' field.",
                hint="Specify a positive settlement amount.",
            )
        )
    else:
        try:
            amt_paise = to_paise(amount_raw)
            if amt_paise <= 0:
                diagnostics.append(
                    Diagnostic(
                        group=group_id,
                        record_type="settlement",
                        record_id=record_id_label,
                        field="amount",
                        message=f"Settlement amount must be positive, got {amount_raw!r}.",
                        hint="Specify a finite number greater than 0.",
                    )
                )
        except (ValueError, TypeError) as exc:
            diagnostics.append(
                Diagnostic(
                    group=group_id,
                    record_type="settlement",
                    record_id=record_id_label,
                    field="amount",
                    message=f"Invalid settlement monetary amount {amount_raw!r}: {exc}",
                    hint="Settlement amounts must be valid finite numbers.",
                )
            )


def validate_file(filepath=DEFAULT_DATA_PATH):
    """Inspect and validate the stored JSON file.

    Returns: (diagnostics, summary_stats)
    """
    path = Path(filepath)
    if not path.exists():
        diag = Diagnostic(
            group=None,
            record_type="store",
            record_id=None,
            field=None,
            message=f"Data store file not found: '{filepath}'.",
            hint="Ensure data file exists or initialize it.",
        )
        return [diag], {"groups_checked": 0, "expenses_checked": 0, "settlements_checked": 0, "errors_found": 1}

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as exc:
        diag = Diagnostic(
            group=None,
            record_type="store",
            record_id=None,
            field=None,
            message=f"Failed to read data file '{filepath}': {exc}",
            hint="Check file read permissions.",
        )
        return [diag], {"groups_checked": 0, "expenses_checked": 0, "settlements_checked": 0, "errors_found": 1}

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        diag = Diagnostic(
            group=None,
            record_type="store",
            record_id=None,
            field=None,
            message=f"Malformed JSON in '{filepath}': line {exc.lineno}, column {exc.colno}: {exc.msg}",
            hint="Correct JSON syntax errors in the data file.",
        )
        return [diag], {"groups_checked": 0, "expenses_checked": 0, "settlements_checked": 0, "errors_found": 1}

    return validate_store(data)

