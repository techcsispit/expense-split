"""Expense splitting and debt calculation logic."""

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

# The smallest monetary unit used when converting amounts to whole paise.
_SCALE = Decimal("0.01")


def split_equally(amount, num_people):
    """Splits a total amount equally among a given number of people."""
    if num_people <= 0:
        raise ValueError("Number of people must be greater than 0")

    each = int(amount) // num_people
    return {"each": each}


def split_by_share(amount, shares):
    """Splits an amount according to given proportion weights in a dictionary.

    The split is done in integer paise so the returned shares always add up to
    exactly ``amount``.  Each participant first receives the floor of their
    proportional share, then any leftover paise are handed out one at a time to
    the participants with the largest fractional remainders (ties broken by
    their order in ``shares``), which keeps the result deterministic.
    """
    # Every individual weight must be a positive share.  Checking only the
    # total would let a zero or negative weight through whenever the other
    # weights make the sum positive, producing zero or negative allocations.
    for person, weight in shares.items():
        if weight <= 0:
            raise ValueError(
                f"Weight for {person} must be greater than 0, got {weight}"
            )

    total_shares = sum(shares.values())
    if total_shares <= 0:
        raise ValueError("Total shares must be greater than 0")

    amount_paise = int(
        (Decimal(str(amount)) / _SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )

    # Give everyone the floor of their proportional share (an exact integer
    # computation in paise), then record how much each is short of it.
    allocations = {}
    remainders = []
    unallocated = amount_paise
    for person, weight in shares.items():
        exact = Decimal(amount_paise) * Decimal(str(weight)) / Decimal(str(total_shares))
        base = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        allocations[person] = base
        unallocated -= base
        remainders.append((exact - base, person))

    # Hand out the leftover paise to the largest fractional remainders first;
    # ties are broken by the participant's order in ``shares`` so the result is
    # deterministic.
    remainders.sort(key=lambda item: (-item[0],))
    for i in range(unallocated):
        allocations[remainders[i % len(remainders)][1]] += 1

    result = {}
    for person in shares:
        result[person] = float(Decimal(allocations[person]) * _SCALE)
    return result


def _expense_shares_paise(expense, members):
    """Returns each member's share of an expense in paise."""
    amount = expense.get("amount", 0.0)
    amount_paise = int(round(amount * 100))

    if expense.get("split") == "share":
        shares = split_by_share(amount, expense.get("shares", {}))
        return {
            member: int(round(shares.get(member, 0.0) * 100))
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
        amount_paise = int(round(exp.get("amount", 0.0) * 100))

        balances[paid_by] += amount_paise

        for m, share in _expense_shares_paise(exp, members).items():
            balances[m] -= share

    for st in group_data.get("settlements", []):
        payer = st["from"]
        receiver = st["to"]
        amount_paise = int(round(st["amount"] * 100))
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
        amount_paise = int(round(st["amount"] * 100))
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
    """Returns the smallest list of payments that clears all net balances."""
    balances = get_net_balances(group_data)

    # Filter out participants with zero balance
    non_zero = [(person, bal) for person, bal in balances.items() if bal != 0]
    if not non_zero:
        return []

    # Separate into positive (creditors) and negative (debtors) balances in paise
    # bal > 0: creditor, bal < 0: debtor
    # Group participants into independent zero-sum subsets to maximize K,
    # where total participants N - K gives minimum number of transactions.

    def solve_subset(members_with_bal):
        """Settle a zero-sum subset greedily/sequentially using debt matching."""
        debtors = [[p, -b] for p, b in members_with_bal if b < 0]
        creditors = [[p, b] for p, b in members_with_bal if b > 0]

        # Sort deterministically for consistent output
        debtors.sort(key=lambda x: (-x[1], x[0]))
        creditors.sort(key=lambda x: (-x[1], x[0]))

        sub_payments = []
        i = 0
        j = 0
        while i < len(debtors) and j < len(creditors):
            debtor, d_amount = debtors[i]
            creditor, c_amount = creditors[j]

            settle_amount = min(d_amount, c_amount)
            if settle_amount > 0:
                sub_payments.append({
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
        return sub_payments

    n = len(non_zero)
    # Find a partition into maximum number of zero-sum subsets
    # Memoization cache for subset problem
    memo = {}

    def get_max_subsets(mask):
        if mask == 0:
            return 0, []
        if mask in memo:
            return memo[mask]

        # We can fix the lowest set bit in mask to avoid redundant symmetric partitions
        lowest_bit = mask & -mask
        submask = (mask - 1) & mask

        best_count = -1
        best_subsets = []

        # Iterate through all submasks containing lowest_bit
        curr = submask
        while True:
            candidate_mask = curr | lowest_bit
            # Check if candidate_mask sums to 0
            subset_sum = sum(non_zero[idx][1] for idx in range(n) if (candidate_mask & (1 << idx)))
            if subset_sum == 0:
                rem_count, rem_subsets = get_max_subsets(mask ^ candidate_mask)
                if 1 + rem_count > best_count:
                    best_count = 1 + rem_count
                    best_subsets = [candidate_mask] + rem_subsets
            if curr == 0:
                break
            curr = (curr - 1) & submask

        memo[mask] = (best_count, best_subsets)
        return memo[mask]

    full_mask = (1 << n) - 1
    _, optimal_subsets = get_max_subsets(full_mask)

    payments = []
    for s_mask in optimal_subsets:
        subset_members = [non_zero[idx] for idx in range(n) if (s_mask & (1 << idx))]
        payments.extend(solve_subset(subset_members))

    # Sort payments for deterministic output (by from, then to)
    payments.sort(key=lambda p: (p["from"], p["to"]))
    return payments
