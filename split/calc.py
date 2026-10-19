
"""Expense splitting and debt calculation logic."""

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

# The smallest monetary unit used when converting amounts to whole paise.
_SCALE = Decimal("0.01")


def to_paise(amount):
    """Convert a rupee amount to integer paise using ROUND_HALF_UP.

    Supported types: int, float, str, and Decimal.
    Raises TypeError for unsupported types and ValueError for invalid or
    non-finite amounts.
    """
    if (
        amount is None
        or isinstance(amount, bool)
        or not isinstance(amount, (int, float, str, Decimal))
    ):
        raise TypeError(
            f"Unsupported type for monetary amount: {type(amount).__name__}"
        )

    if isinstance(amount, Decimal):
        decimal_amount = amount
    elif isinstance(amount, str):
        try:
            decimal_amount = Decimal(amount.strip())
        except Exception as exc:
            raise ValueError(
                f"Invalid monetary amount: {amount!r}"
            ) from exc
    else:
        try:
            decimal_amount = Decimal(str(amount))
        except Exception as exc:
            raise ValueError(
                f"Invalid monetary amount: {amount!r}"
            ) from exc

    if not decimal_amount.is_finite():
        raise ValueError(
            f"Monetary amount must be finite, got {amount!r}"
        )

    paise = (
        decimal_amount * Decimal("100")
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    return int(paise)


def split_equally(amount, num_people):
    """Split a total amount equally among a given number of people."""
    if num_people <= 0:
        raise ValueError("Number of people must be greater than 0")

    each = int(amount) // num_people
    return {"each": each}


def _split_by_share_paise(amount, shares):
    """Allocate an amount proportionally and return integer paise per person.

    Any leftover paise are distributed by largest fractional remainder.
    Ties are resolved by the original order of participants.
    """
    for person, weight in shares.items():
        if weight <= 0:
            raise ValueError(
                f"Weight for {person} must be greater than 0, got {weight}"
            )

    decimal_shares = {
        person: Decimal(str(weight))
        for person, weight in shares.items()
    }
    total_shares = sum(decimal_shares.values(), Decimal("0"))
    if total_shares <= 0:
        raise ValueError("Total shares must be greater than 0")

    amount_paise = to_paise(amount)

    allocations = {}
    remainders = []
    unallocated = amount_paise

    for person, weight in decimal_shares.items():
        exact = (
            Decimal(amount_paise)
            * weight
            / total_shares
        )
        base = int(exact.to_integral_value(rounding=ROUND_FLOOR))

        allocations[person] = base
        unallocated -= base
        remainders.append((exact - base, person))

    remainders.sort(key=lambda item: -item[0])

    for i in range(unallocated):
        person = remainders[i % len(remainders)][1]
        allocations[person] += 1

    return allocations


def split_by_share(amount, shares):
    """Split an amount proportionally while preserving its exact total.

    Returns the existing public interface of participant amounts in rupees.
    Internally, allocations are calculated in integer paise.
    """
    allocations = _split_by_share_paise(amount, shares)

    return {
        person: float(Decimal(allocations[person]) * _SCALE)
        for person in shares
    }


def _expense_shares_paise(expense, members):
    """Return each member's expense share in integer paise."""
    amount = expense.get("amount", 0.0)
    amount_paise = to_paise(amount)

    if expense.get("split") == "share":
        allocated = _split_by_share_paise(
            amount, expense.get("shares", {})
        )
        return {
            member: allocated.get(member, 0)
            for member in members
        }

    if not members:
        return {}

    base_share = amount_paise // len(members)
    remainder = amount_paise % len(members)

    return {
        member: base_share + (1 if index < remainder else 0)
        for index, member in enumerate(members)
    }


def get_net_balances(group_data):
    """Calculate net balances in integer paise.

    Positive means the person is owed money; negative means they owe money.
    """
    members = group_data.get("members", [])
    if not members:
        return {}

    balances = {member: 0 for member in members}

    for expense in group_data.get("expenses", []):
        paid_by = expense.get("paid_by")
        amount_paise = to_paise(expense.get("amount", 0.0))

        balances[paid_by] += amount_paise

        for member, share in _expense_shares_paise(
            expense, members
        ).items():
            balances[member] -= share

    for settlement in group_data.get("settlements", []):
        payer = settlement["from"]
        receiver = settlement["to"]
        amount_paise = to_paise(settlement["amount"])

        # Ignore zero or negative settlement amounts.
        if amount_paise <= 0:
            continue

        balances[payer] += amount_paise
        balances[receiver] -= amount_paise

    return balances


def who_owes(group_data):
    """Calculates netted pairwise debts between members.

    Pairwise netting can create circular obligations after somebody follows a
    globally netted settlement plan.  Such cycles have no effect on anyone's
    net balance, so remove them before reporting the remaining debts.
    """
    members = group_data.get("members", [])
    if not members:
        return []

    owes = {
        member: {other: 0 for other in members}
        for member in members
    }

    for expense in group_data.get("expenses", []):
        paid_by = expense.get("paid_by")

        for member, share in _expense_shares_paise(
            expense, members
        ).items():
            if member == paid_by:
                continue

            owes[member][paid_by] += share

    for settlement in group_data.get("settlements", []):
        payer = settlement["from"]
        receiver = settlement["to"]
        amount_paise = to_paise(settlement["amount"])

        if amount_paise <= 0:
            continue

        owes[payer][receiver] -= amount_paise

    edges = {member: {} for member in members}
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            member1 = members[i]
            member2 = members[j]
            net = owes[member1][member2] - owes[member2][member1]
            if net > 0:
                edges[member1][member2] = net
            elif net < 0:
                edges[member2][member1] = -net

    def find_cycle():
        """Returns one directed cycle as edge pairs, or None if acyclic."""
        visited = set()
        active = set()
        path = []

        def visit(person):
            visited.add(person)
            active.add(person)
            path.append(person)

            for receiver, amount in edges[person].items():
                if amount <= 0:
                    continue
                if receiver in active:
                    start = path.index(receiver)
                    nodes = path[start:] + [receiver]
                    return list(zip(nodes, nodes[1:]))
                if receiver not in visited:
                    cycle = visit(receiver)
                    if cycle:
                        return cycle

            path.pop()
            active.remove(person)
            return None

        for member in members:
            if member not in visited:
                cycle = visit(member)
                if cycle:
                    return cycle
        return None

    while True:
        cycle = find_cycle()
        if not cycle:
            break
        cancelled = min(edges[payer][receiver] for payer, receiver in cycle)
        for payer, receiver in cycle:
            edges[payer][receiver] -= cancelled

    debts = []
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            member1 = members[i]
            member2 = members[j]
            if edges[member1].get(member2, 0) > 0:
                debts.append({"from": member1, "to": member2, "amount": edges[member1][member2] / 100.0})
            elif edges[member2].get(member1, 0) > 0:
                debts.append({"from": member2, "to": member1, "amount": edges[member2][member1] / 100.0})

    return debts


def settle_plan(group_data):
    """Return a minimum-payment plan that clears all net balances.

    The payment dictionary format is a list of:
        {"from": name, "to": name, "amount": paise_value / 100.0}

    The minimum number of payments needed to settle non-zero balances is
    N - K, where N is the number of participants with non-zero balances and
    K is the maximum number of disjoint zero-sum subsets.

    Optimizations applied:
    1. Optimal Pair Extraction (Symmetry Reduction):
       Any zero-sum subset with non-zero balances requires >= 2 members.
       Therefore, max K <= N // 2. Opposite pairs (person A with +X, person B
       with -X) achieve 1 subset per 2 participants, reaching the theoretical
       maximum ratio 1/2. Extracting these pairs provably preserves global
       optimality while drastically shrinking N.
    2. Dynamic Programming Subset Sums:
       Subset sums for remaining elements are precomputed in O(2^m) via bitwise
       transitions, replacing repeated O(m) generator recalculations with O(1)
       table lookups.
    3. Filtered Zero-Sum Submask Indexing:
       Submasks are pre-filtered to include only genuine zero-sum subsets and
       indexed by their least significant bit (LSB). Search branches only visit
       candidate zero-sum subsets rather than all 3^m submasks.
    4. Exact-Search Threshold and Optional Approximation Mode:
       If the remaining participants m <= 18 (or total N <= 20 with pairs),
       an exact optimal search is executed. For groups exceeding this threshold,
       or when `group_data.get("approximate")` is True, an explicit greedy
       approximation mode is used, guaranteeing valid settlements with exact
       integer paise arithmetic while warning/documenting the heuristic trade-off.
    """
    balances = get_net_balances(group_data)

    # Sort participants by name for deterministic ordering
    non_zero = sorted(
        [(person, balance) for person, balance in balances.items() if balance != 0],
        key=lambda item: item[0],
    )

    if not non_zero:
        return []

    def solve_subset(members_with_balances):
        """Settle one zero-sum subset using deterministic debt matching."""
        debtors = [
            [person, -balance]
            for person, balance in members_with_balances
            if balance < 0
        ]
        creditors = [
            [person, balance]
            for person, balance in members_with_balances
            if balance > 0
        ]

        debtors.sort(key=lambda item: (-item[1], item[0]))
        creditors.sort(key=lambda item: (-item[1], item[0]))

        payments = []
        i = 0
        j = 0

        while i < len(debtors) and j < len(creditors):
            debtor, debt_amount = debtors[i]
            creditor, credit_amount = creditors[j]

            payment = min(debt_amount, credit_amount)

            if payment > 0:
                payments.append({
                    "from": debtor,
                    "to": creditor,
                    "amount": payment / 100.0,
                })

            debtors[i][1] -= payment
            creditors[j][1] -= payment

            if debtors[i][1] == 0:
                i += 1
            if creditors[j][1] == 0:
                j += 1

        return payments

    # ------------------------------------------------------------------
    # Step 1: Extract opposite pairs (+X, -X) provably preserving optimal K
    # ------------------------------------------------------------------
    pos = {}
    neg = {}
    for p, b in non_zero:
        if b > 0:
            pos.setdefault(b, []).append((p, b))
        else:
            neg.setdefault(-b, []).append((p, b))

    pairs = []
    used_pos = {}
    used_neg = {}

    for val in sorted(pos.keys()):
        if val in neg:
            p_list = pos[val]
            n_list = neg[val]
            count = min(len(p_list), len(n_list))
            for k in range(count):
                pairs.append([p_list[k], n_list[k]])
            used_pos[val] = count
            used_neg[val] = count

    rem = []
    for val in sorted(pos.keys()):
        rem.extend(pos[val][used_pos.get(val, 0):])
    for val in sorted(neg.keys()):
        rem.extend(neg[val][used_neg.get(val, 0):])

    # Re-sort remaining participants deterministically
    rem.sort(key=lambda item: item[0])

    m = len(rem)
    subsets = list(pairs)

    use_approx = group_data.get("approximate", False)
    EXACT_THRESHOLD = 18  # Exact search practical limit for remaining items

    if m > 0:
        if not use_approx and m <= EXACT_THRESHOLD:
            # Step 2: Exact search via filtered zero-sum subset DP
            size = 1 << m
            subset_sum = [0] * size
            for mask in range(1, size):
                lsb = mask & -mask
                bit_idx = lsb.bit_length() - 1
                subset_sum[mask] = subset_sum[mask ^ lsb] + rem[bit_idx][1]

            zero_masks = [mask for mask in range(1, size) if subset_sum[mask] == 0]
            # Prioritize smaller subsets (higher K) and break ties deterministically
            zero_masks.sort(key=lambda mask: (mask.bit_count(), mask))

            masks_by_lsb = [[] for _ in range(m)]
            for mask in zero_masks:
                lsb = mask & -mask
                bit_idx = lsb.bit_length() - 1
                masks_by_lsb[bit_idx].append(mask)

            memo = {}

            def get_max_subsets(mask):
                if mask == 0:
                    return 0, []
                if mask in memo:
                    return memo[mask]

                lsb = mask & -mask
                bit_idx = lsb.bit_length() - 1

                best_k = -1
                best_parts = []

                for cand in masks_by_lsb[bit_idx]:
                    if (cand & mask) == cand:
                        rem_k, rem_parts = get_max_subsets(mask ^ cand)
                        if 1 + rem_k > best_k:
                            best_k = 1 + rem_k
                            best_parts = [cand] + rem_parts

                memo[mask] = (best_k, best_parts)
                return memo[mask]

            _, optimal_submasks = get_max_subsets((1 << m) - 1)
            for s_mask in optimal_submasks:
                subsets.append([rem[i] for i in range(m) if s_mask & (1 << i)])
        else:
            # Step 3: Heuristic approximation mode for very large groups
            # Greedily search small zero-sum subsets (size 3 and 4), settle remainder
            from itertools import combinations
            current_rem = list(rem)
            while len(current_rem) >= 3:
                found = False
                for r in range(3, min(5, len(current_rem) + 1)):
                    for combo in combinations(current_rem, r):
                        if sum(item[1] for item in combo) == 0:
                            subsets.append(list(combo))
                            for item in combo:
                                current_rem.remove(item)
                            found = True
                            break
                    if found:
                        break
                if not found:
                    break

            if current_rem:
                subsets.append(current_rem)

    payments = []
    for subset_members in subsets:
        payments.extend(solve_subset(subset_members))

    payments.sort(key=lambda payment: (
        payment["from"],
        payment["to"],
    ))

    return payments
