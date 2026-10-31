"""Category classification, spending distribution, and participant contribution analysis."""

from decimal import Decimal
from typing import Any, Dict, List, Optional
from split.calc import to_paise

# Standard expense category definitions with descriptive labels
STANDARD_CATEGORIES = {
    "food": "Food & Dining",
    "travel": "Travel & Commute",
    "stay": "Accommodation & Stay",
    "utilities": "Utilities & Bills",
    "entertainment": "Entertainment & Outings",
    "general": "General & Miscellaneous",
}

# Heuristics for auto-detecting categories from expense descriptions
CATEGORY_KEYWORDS = {
    "food": [
        "food", "dinner", "lunch", "breakfast", "snack", "cafe", "coffee", "tea",
        "pizza", "burger", "biryani", "swiggy", "zomato", "restaurant", "canteen",
        "grocery", "groceries", "milk", "vegetables", "fruits", "drinks"
    ],
    "entertainment": [
        "movie", "cinema", "netflix", "prime", "hotstar", "game", "gaming", "outing",
        "concert", "party", "club", "bowling", "tickets", "ticket"
    ],
    "stay": [
        "stay", "hotel", "hostel", "airbnb", "resort", "rent", "room", "lodge", "booking"
    ],
    "utilities": [
        "utility", "utilities", "wifi", "internet", "electricity", "water", "bill",
        "gas", "cylinder", "recharge", "maintenance"
    ],
    "travel": [
        "travel", "cab", "uber", "ola", "auto", "taxi", "flight", "train", "metro",
        "bus", "petrol", "diesel", "fuel", "toll", "parking"
    ],
}


def detect_category(description: str, explicit_category: Optional[str] = None) -> str:
    """Determine the canonical category for an expense.

    If an explicit category is provided and valid, normalize and return it.
    Otherwise, infer from keyword matches in the description, defaulting to 'general'.
    """
    if explicit_category:
        norm = explicit_category.strip().lower()
        if norm in STANDARD_CATEGORIES:
            return norm

    desc_lower = (description or "").lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in desc_lower for kw in keywords):
            return cat

    return "general"


def analyze_categories(group_data: Dict[str, Any]) -> Dict[str, Any]:
    """Compute aggregate category spending metrics for a group.

    Returns a structured dictionary containing:
    - total_spent_paise: Overall group spending in paise
    - category_breakdown: Mapping of category -> {
          label: str,
          total_paise: int,
          percentage: float,
          expense_count: int,
          participants: Dict[str, int] (paise paid by each member for this category)
      }
    - top_category: Category name with the highest spending
    """
    expenses = group_data.get("expenses", [])
    total_spent_paise = 0
    category_totals: Dict[str, int] = {cat: 0 for cat in STANDARD_CATEGORIES}
    category_counts: Dict[str, int] = {cat: 0 for cat in STANDARD_CATEGORIES}
    category_participants: Dict[str, Dict[str, int]] = {cat: {} for cat in STANDARD_CATEGORIES}

    for exp in expenses:
        raw_amt = exp.get("amount", 0)
        try:
            amt_paise = to_paise(raw_amt)
        except Exception:
            continue

        if amt_paise <= 0:
            continue

        total_spent_paise += amt_paise
        cat = detect_category(exp.get("description", ""), exp.get("category"))
        category_totals[cat] = category_totals.get(cat, 0) + amt_paise
        category_counts[cat] = category_counts.get(cat, 0) + 1

        # Track payers
        paid_by = exp.get("paid_by")
        if isinstance(paid_by, dict):
            for payer, payer_amt in paid_by.items():
                try:
                    p_paise = to_paise(payer_amt)
                except Exception:
                    continue
                p_norm = payer.strip().lower()
                category_participants[cat][p_norm] = (
                    category_participants[cat].get(p_norm, 0) + p_paise
                )
        elif isinstance(paid_by, str) and paid_by.strip():
            p_norm = paid_by.strip().lower()
            category_participants[cat][p_norm] = (
                category_participants[cat].get(p_norm, 0) + amt_paise
            )

    breakdown = {}
    top_cat = None
    max_cat_paise = -1

    for cat, label in STANDARD_CATEGORIES.items():
        c_paise = category_totals.get(cat, 0)
        if c_paise > max_cat_paise and c_paise > 0:
            max_cat_paise = c_paise
            top_cat = cat

        pct = round((c_paise / total_spent_paise * 100), 1) if total_spent_paise > 0 else 0.0
        breakdown[cat] = {
            "label": label,
            "total_paise": c_paise,
            "total_rupees": c_paise / 100.0,
            "percentage": pct,
            "expense_count": category_counts.get(cat, 0),
            "participants": category_participants.get(cat, {}),
        }

    return {
        "total_spent_paise": total_spent_paise,
        "total_spent_rupees": total_spent_paise / 100.0,
        "top_category": top_cat,
        "breakdown": breakdown,
    }
