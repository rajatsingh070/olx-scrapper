import datetime
import re


def format_price(price: str) -> int:
    """
    Returns price as integer. Removes all non-digit characters, so "₹ 1,25,000" -> 125000.
    :param price: price string
    :return: formatted price
    """
    price = re.sub(r'\D', '', price or "")
    return int(price) if price else 0


def format_inr(amount: int) -> str:
    """
    Formats an amount with Indian digit grouping: 1250000 -> "₹ 12,50,000".
    :param amount: amount in rupees
    :return: formatted amount
    """
    digits = str(abs(int(amount)))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
        digits = f"{head},{tail}"
    sign = "-" if amount < 0 else ""
    return f"{sign}₹ {digits}"


def format_listing_date(value: str) -> str:
    """
    Returns a listing date as "12 Sep 2026". ISO timestamps from the olx.in API are
    converted; anything else ("Today", "3 days ago") is returned unchanged.
    :param value: date string
    :return: formatted date
    """
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%d %b %Y")


def format_location_date(location_date: str) -> tuple[str, str]:
    """
    Splits a "Location - Date" string into its two parts.
    :param location_date: location and date string
    :return: location and date
    """
    location, date = location_date.split(" - ")[:2]
    return location.strip(), date.strip()
