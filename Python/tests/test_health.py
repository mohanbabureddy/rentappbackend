import unittest
from unittest import mock

from app import app as flask_app


class HealthEndpointTest(unittest.TestCase):
    def test_touches_the_database_and_reports_ok(self):
        db = mock.Mock()
        with mock.patch("app.routes.get_db", return_value=db):
            r = flask_app.test_client().get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"status": "ok", "database": "up"})
        # The point of the endpoint: a real query, so Supabase sees activity.
        db.execute.assert_called_once()
        self.assertEqual(str(db.execute.call_args[0][0]), "SELECT 1")

    def test_reports_503_when_the_database_is_down(self):
        db = mock.Mock()
        db.execute.side_effect = RuntimeError("connection refused")
        with mock.patch("app.routes.get_db", return_value=db):
            r = flask_app.test_client().get("/api/health")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["database"], "down")

    def test_needs_no_login_and_returns_no_data(self):
        with mock.patch("app.routes.get_db", return_value=mock.Mock()):
            r = flask_app.test_client().get("/api/health")   # no Authorization header
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.get_json()), {"status", "database"})


if __name__ == "__main__":
    unittest.main()
