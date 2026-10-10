"""Expense splitting and debt calculation logic."""

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

# The smallest monetary unit used when converting amounts to whole paise.
_SCALE = Decimal("0.01")


def to_paise(amount):
    """Converts a rupee amount (int, float, str, or Decimal) to integer paise.

    Uses Decimal with ROUND_HALF_UP to ensure consistent monetary rounding
    across all operations. Non-finite values (NaN, Inf) and invalid inputs
    raise ValueError or TypeError.
    """
    if amount is None or isinstance(amount, bool) or not isinstance(amount, (int, float, str, Decimal)):
        raise TypeError(f"Unsupported type for monetary amount: {type(amount).__name__}")

    if isinstance(amount, Decimal):
        d = amount
    elif isinstance(amount, str):
        try:
            d = Decimal(amount.strip())
        except Exception as e:
            raise ValueError(f"Invalid monetary amount: {amount!r}") from e
    else:  # int or float
        try:
            d = Decimal(str(amount))
        except Exception as e:
            raise ValueError(f"Invalid monetary amount: {amount!r}") from e

    if not d.is_finite():
        raise ValueError(f"Monetary amount must be finite, got {amount!r}")

    paise_decimal = (d * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(paise_decimal)


def split_equally(amount, num_people):
    """Splits a total amount equally among a given number of people."""
    if num_people <= 0:
        raise ValueError("Number of people must be greater than 0")

    each = int(amount) // num_people
    return {"each": each}


def _split_by_share_paise(amount, shares):
    """Splits an amount according to given proportion weights in a dictionary,
    returning each participant's allocation directly in integer paise.
    """
    for person, weight in shares.items():
        if weight <= 0:
            raise ValueError(
                f"Weight for {person} must be greater than 0, got {weight}"
            )

    total_shares = sum(shares.values())
    if total_shares <= 0:
        raise ValueError("Total shares must be greater than 0")

    amount_paise = to_paise(amount)

    allocations = {}
    remainders = []
    unallocated = amount_paise
    for person, weight in shares.items():
        exact = Decimal(amount_paise) * Decimal(str(weight)) / Decimal(str(total_shares))
        base = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        allocations[person] = base
        unallocated -= base
        remainders.append((exact - base, person))

    remainders.sort(key=lambda item: (-item[0],))
    for i in range(unallocated):
        allocations[remainders[i % len(remainders)][1]] += 1

    return allocations


def split_by_share(amount, shares):
    """Splits an amount according to given proportion weights in a dictionary.

    The split is done in integer paise so the returned shares always add up to
    exactly ``amount``.  Each participant first receives the floor of their
    proportional share, then any leftover paise are handed out one at a time to
    the participants with the largest fractional remainders (ties broken by
    their order in ``shares``), which keeps the result deterministic.
    """
    allocations = _split_by_share_paise(amount, shares)
    result = {}
    for person in shares:
        result[person] = float(Decimal(allocations[person]) * _SCALE)
    return result


def _expense_shares_paise(expense, members):
    """Returns each member's share of an expense in paise."""
    amount = expense.get("amount", 0.0)
    amount_paise = to_paise(amount)

    if expense.get("split") == "share":
        allocated = _split_by_share_paise(amount, expense.get("shares", {}))
        return {
            member: allocated.get(member, 0)
            for member in members
        }

    base_share = amount_paise // len(members)
    remainder = amount_paise % len(members)
    return {
        member: base_share + (1 if index < remainder else 0)
        for index, member in enumerate(members)
    }


def get_net_balances(group_data):
    """Calculates each person's net balance in paise from expenses and settlements.
    Positive means they are owed money, negative means they owe money.
    """
    members = group_data.get("members", [])
    if not members:
        return {}

    balances = {m: 0 for m in members}
    
    for exp in group_data.get("expenses", []):
        paid_by = exp.get("paid_by")
        amount_paise = to_paise(exp.get("amount", 0.0))
        
        balances[paid_by] += amount_paise

        for m, share in _expense_shares_paise(exp, members).items():
            balances[m] -= share
            
    for st in group_data.get("settlements", []):
        payer = st["from"]
        receiver = st["to"]
        amount_paise = to_paise(st["amount"])
        # Ignore invalid settlement amounts (must be positive)
        if amount_paise <= 0:
            continue
        balances[payer] += amount_paise
        balances[receiver] -= amount_paise

    return balances


def who_owes(group_data):
    """Calculates netted pairwise debts between members."""
    members = group_data.get("members", [])
    if not members:
        return []
        
    owes = {m: {m2: 0 for m2 in members} for m in members}
    
    for exp in group_data.get("expenses", []):
        paid_by = exp.get("paid_by")
        for m, share in _expense_shares_paise(exp, members).items():
            if m == paid_by:
                continue
            owes[m][paid_by] += share

    for st in group_data.get("settlements", []):
        payer = st["from"]
        receiver = st["to"]
        amount_paise = to_paise(st["amount"])
        # Ignore invalid settlement amounts (must be positive)
        if amount_paise <= 0:
            continue
        owes[payer][receiver] -= amount_paise

    debts = []
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            m1 = members[i]
            m2 = members[j]
            net = owes[m1][m2] - owes[m2][m1]
            if net > 0:
                debts.append({"from": m1, "to": m2, "amount": net / 100.0})
            elif net < 0:
                debts.append({"from": m2, "to": m1, "amount": -net / 100.0})
                
    return debts


def settle_plan(group_data):
    """Returns the smallest list of payments that clears everything."""
    balances = get_net_balances(group_data)
    
    debtors = []
    creditors = []
    
    for person, balance in balances.items():
        if balance < 0:
            debtors.append([person, -balance])
        elif balance > 0:
            creditors.append([person, balance])
            
    debtors.sort(key=lambda x: x[1], reverse=True)
    creditors.sort(key=lambda x: x[1], reverse=True)
    
    payments = []
    i = 0
    j = 0
    while i < len(debtors) and j < len(creditors):
        debtor, d_amount = debtors[i]
        creditor, c_amount = creditors[j]
        
        settle_amount = min(d_amount, c_amount)
        
        payments.append({
            "from": debtor,
            "to": creditor,
            "amount": settle_amount / 100.0
        })
        
        debtors[i][1] -= settle_amount
        creditors[j][1] -= settle_amount
        
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
            
    return payments
