import unittest
from datetime import date

from app.vacate import calculate_vacate_date, next_due_date


class NextDueDateTest(unittest.TestCase):
    def test_before_the_10th_stays_in_the_same_month(self):
        self.assertEqual(next_due_date(date(2026, 9, 1)), date(2026, 9, 10))
        self.assertEqual(next_due_date(date(2026, 9, 9)), date(2026, 9, 10))

    def test_on_the_10th_counts_as_the_next_due_date(self):
        self.assertEqual(next_due_date(date(2026, 9, 10)), date(2026, 9, 10))

    def test_after_the_10th_rolls_to_next_month(self):
        self.assertEqual(next_due_date(date(2026, 9, 11)), date(2026, 10, 10))
        self.assertEqual(next_due_date(date(2026, 9, 22)), date(2026, 10, 10))
        self.assertEqual(next_due_date(date(2026, 9, 30)), date(2026, 10, 10))

    def test_rolls_across_a_year_boundary(self):
        self.assertEqual(next_due_date(date(2026, 12, 22)), date(2027, 1, 10))


class CalculateVacateDateTest(unittest.TestCase):
    def test_the_owners_worked_example(self):
        # Sep 22 -> next due Oct 10 -> +1 month = Nov 10 -> -2 days = Nov 8
        self.assertEqual(calculate_vacate_date(date(2026, 9, 22)), date(2026, 11, 8))

    def test_requesting_exactly_on_the_due_date(self):
        # Sep 10 -> next due Sep 10 (today itself) -> +1 month = Oct 10 -> -2 days = Oct 8
        self.assertEqual(calculate_vacate_date(date(2026, 9, 10)), date(2026, 10, 8))

    def test_requesting_the_day_after_the_due_date(self):
        # Sep 11 -> next due Oct 10 -> +1 month = Nov 10 -> -2 days = Nov 8
        self.assertEqual(calculate_vacate_date(date(2026, 9, 11)), date(2026, 11, 8))

    def test_across_a_year_boundary(self):
        # Dec 15 -> next due Jan 10 -> +1 month = Feb 10 -> -2 days = Feb 8
        self.assertEqual(calculate_vacate_date(date(2026, 12, 15)), date(2027, 2, 8))

    def test_short_february_is_handled_by_the_calendar_correctly(self):
        # Jan 15 -> next due Feb 10 -> +1 month = Mar 10 -> -2 days = Mar 8
        self.assertEqual(calculate_vacate_date(date(2026, 1, 15)), date(2026, 3, 8))

    def test_a_full_year_of_request_dates_always_lands_on_the_8th(self):
        # The 8th-of-some-month falls out of the rule mechanically (10th minus 2 days)
        # regardless of which day within the month the tenant actually requests.
        for month in range(1, 13):
            for day in (1, 10, 28):
                result = calculate_vacate_date(date(2026, month, day))
                self.assertEqual(result.day, 8, f"request on {2026}-{month}-{day}")


if __name__ == "__main__":
    unittest.main()
