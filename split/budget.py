"""Budget tracking, burn rate calculation, and overspend threshold alerts for groups."""

from typing import Any, Dict, Optional
from split.calc import to_paise

# Threshold percentages
ALERT_THRESHOLD_PCT = 80.0
LIMIT_THRESHOLD_PCT = 100.0


def set_group_budget(group_data: Dict[str, Any], budget_amount: Any) -> int:
    """Set the total budget for a group in integer paise.

    Raises ValueError if amount is negative or non-numeric.
    Returns the budget in paise.
    """
    budget_paise = to_paise(budget_amount)
    if budget_paise < 0:
        raise ValueError("Budget amount cannot be negative.")
    group_data["budget_paise"] = budget_paise
    return budget_paise


def clear_group_budget(group_data: Dict[str, Any]) -> None:
    """Remove the budget constraint from a group."""
    group_data.pop("budget_paise", None)
    group_data.pop("budget", None)


def evaluate_budget_status(group_data: Dict[str, Any], total_spent_paise: int) -> Dict[str, Any]:
    """Evaluate current spending against group budget.

    Returns:
    - has_budget: bool
    - budget_paise: int
    - budget_rupees: float
    - spent_paise: int
    - spent_rupees: float
    - remaining_paise: int
    - remaining_rupees: float
    - usage_percentage: float
    - status: 'under' | 'warning' (>=80%) | 'exceeded' (>=100%)
    - message: Descriptive status message
    """
    raw_budget = group_data.get("budget_paise")
    if raw_budget is None and "budget" in group_data:
        try:
            raw_budget = to_paise(group_data["budget"])
        except Exception:
            raw_budget = None

    if raw_budget is None or raw_budget <= 0:
        return {
            "has_budget": False,
            "budget_paise": 0,
            "budget_rupees": 0.0,
            "spent_paise": total_spent_paise,
            "spent_rupees": total_spent_paise / 100.0,
            "remaining_paise": 0,
            "remaining_rupees": 0.0,
            "usage_percentage": 0.0,
            "status": "none",
            "message": "No budget configured for this group.",
        }

    budget_paise = int(raw_budget)
    remaining_paise = budget_paise - total_spent_paise
    usage_pct = round((total_spent_paise / budget_paise * 100.0), 1)

    if usage_pct >= LIMIT_THRESHOLD_PCT:
        status = "exceeded"
        over_rupees = abs(remaining_paise) / 100.0
        message = f"Budget EXCEEDED by ₹{over_rupees:.2f} ({usage_pct}% used)!"
    elif usage_pct >= ALERT_THRESHOLD_PCT:
        status = "warning"
        message = f"Budget WARNING: {usage_pct}% of budget utilized (₹{remaining_paise / 100.0:.2f} remaining)."
    else:
        status = "under"
        message = f"Within budget: {usage_pct}% utilized (₹{remaining_paise / 100.0:.2f} remaining)."

    return {
        "has_budget": True,
        "budget_paise": budget_paise,
        "budget_rupees": budget_paise / 100.0,
        "spent_paise": total_spent_paise,
        "spent_rupees": total_spent_paise / 100.0,
        "remaining_paise": remaining_paise,
        "remaining_rupees": remaining_paise / 100.0,
        "usage_percentage": usage_pct,
        "status": status,
        "message": message,
    }
