import unittest
from datetime import datetime, timedelta, timezone

from app.models import Complaint, utc_now


class UtcNowTest(unittest.TestCase):
    def test_is_naive_utc_like_the_old_utcnow(self):
        now = utc_now()
        self.assertIsNone(now.tzinfo)  # the DateTime columns store naive UTC
        self.assertLess(abs(now - datetime.now(timezone.utc).replace(tzinfo=None)), timedelta(seconds=5))

    def test_a_column_default_produces_a_naive_utc_time(self):
        default = Complaint.__table__.c.created_date.default.arg
        self.assertIsNone(default(None).tzinfo)


if __name__ == "__main__":
    unittest.main()
