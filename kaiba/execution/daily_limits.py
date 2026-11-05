"""Explicit owner-configured unlimited daily allowances; missing values stay bounded."""
from decimal import Decimal, InvalidOperation


def daily_amount(value):
    if value == "unlimited":
        return None
    if isinstance(value,bool) or value is None:
        raise ValueError("invalid_daily_allowance")
    try:
        amount=Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid_daily_allowance") from exc
    if not amount.is_finite() or amount<0:
        raise ValueError("invalid_daily_allowance")
    return amount


def daily_count(value):
    amount=daily_amount(value)
    if amount is None:
        return None
    if amount!=amount.to_integral_value():
        raise ValueError("invalid_daily_count")
    return int(amount)
