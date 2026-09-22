"""Vacate-notice date math, kept separate from any database access so it can be
tested exhaustively with plain dates.

Rule (confirmed with the owner using a worked example, today=Sep 22 -> Nov 8):
    1. Find the next rent due date (the 10th) on or after today.
    2. Add one month's notice.
    3. Subtract 2 days for the room turnover (painting etc.) before the next tenant.
"""
from datetime import date, timedelta

DUE_DAY = 10
NOTICE_MONTHS = 1
TURNOVER_DAYS = 2


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, d.day)


def next_due_date(today: date, due_day: int = DUE_DAY) -> date:
    """The next occurrence of `due_day` on or after `today`."""
    if today.day <= due_day:
        return date(today.year, today.month, due_day)
    return _add_months(date(today.year, today.month, due_day), 1)


def calculate_vacate_date(today: date) -> date:
    due = next_due_date(today)
    notice_end = _add_months(due, NOTICE_MONTHS)
    return notice_end - timedelta(days=TURNOVER_DAYS)
