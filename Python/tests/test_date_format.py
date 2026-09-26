import unittest
from datetime import date, datetime

from app.services import format_dmy


class FormatDmyTest(unittest.TestCase):
    def test_iso_strings_become_day_month_year(self):
        self.assertEqual(format_dmy("2026-09-25"), "25/09/2026")
        self.assertEqual(format_dmy("2026-11-08T10:30:00Z"), "08/11/2026")

    def test_date_objects_too(self):
        self.assertEqual(format_dmy(date(2026, 1, 5)), "05/01/2026")
        self.assertEqual(format_dmy(datetime(2026, 12, 31, 23, 59)), "31/12/2026")

    def test_anything_else_is_left_alone(self):
        self.assertEqual(format_dmy("not a date"), "not a date")
        self.assertEqual(format_dmy(None), "")


if __name__ == "__main__":
    unittest.main()
