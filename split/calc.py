"""Expense splitting and debt calculation logic."""

import math


def split_equally(amount, num_people):
    """Splits a total amount equally among a given number of people."""
    if num_people <= 0:
        raise ValueError("Number of people must be greater than 0")

    each = int(amount) // num_people
    return {"each": each}


def split_by_share(amount, shares):
    """Splits an amount according to given proportion weights in a dictionary."""
    total_shares = sum(shares.values())
    if (not shares or not math.isfinite(total_shares) or total_shares <= 0
            or any(not math.isfinite(weight) or weight <= 0
                   for weight in shares.values())):
        raise ValueError("Total shares must be greater than 0")

    amount_paise = int(round(amount * 100))
    allocations = {}
    fractions = []

    for index, (person, weight) in enumerate(shares.items()):
        exact_share = (amount_paise * weight) / total_shares
        share_paise = int(exact_share)
        allocations[person] = share_paise
        fractions.append((exact_share - share_paise, index, person))

    remainder = amount_paise - sum(allocations.values())
    fractions.sort(key=lambda item: (-item[0], item[1]))
    for _, _, person in fractions[:remainder]:
        allocations[person] += 1

    return {person: value / 100.0 for person, value in allocations.items()}


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
