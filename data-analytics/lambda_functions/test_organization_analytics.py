"""
Local unit tests for organization_analytics — Issue #228.
Mocks psycopg2 and env vars; no real DB needed.

Run with:
    python test_organization_analytics.py
"""

import os
import sys
import json
import unittest
from unittest.mock import MagicMock, patch

sys.modules.setdefault("psycopg2", MagicMock())
sys.modules.setdefault("psycopg2.extras", MagicMock())

MOCK_ENV = {
    "DB_HOST": "localhost",
    "DB_NAME": "testdb",
    "DB_USER": "test",
    "DB_PASSWORD": "test",
    "DB_PORT": "5432",
}

import organization_analytics as oa


REQUIRED_KEYS = {
    "summary",
    "growth_trend",
    "organizations_by_location",
    "organizations_by_size",
    "collaborator_vs_contributor",
    "rating_distribution",
    "organization_type_distribution",
}


# ── filter builders ────────────────────────────────────────────────────────

class TestBuildDateFilter(unittest.TestCase):
    def test_7d(self):
        s, p = oa.build_date_filter("7D")
        self.assertIn("INTERVAL '7 days'", s)
        self.assertEqual(p, [])

    def test_30d(self):
        s, p = oa.build_date_filter("30D")
        self.assertIn("INTERVAL '30 days'", s)

    def test_1y(self):
        s, p = oa.build_date_filter("1Y")
        self.assertIn("INTERVAL '1 year'", s)

    def test_all_or_missing_returns_empty(self):
        for val in ["ALL", "all", None, "", "garbage"]:
            s, p = oa.build_date_filter(val)
            self.assertEqual((s, p), ("", []))

    def test_custom_with_both_dates(self):
        s, p = oa.build_date_filter("CUSTOM", "2026-01-01", "2026-06-30")
        self.assertIn("BETWEEN %s AND %s", s)
        self.assertEqual(p, ["2026-01-01", "2026-06-30"])

    def test_custom_missing_dates_falls_back(self):
        self.assertEqual(oa.build_date_filter("CUSTOM"), ("", []))
        self.assertEqual(oa.build_date_filter("CUSTOM", "2026-01-01"), ("", []))

    def test_custom_column_override(self):
        s, _ = oa.build_date_filter("7D", column="o.updated_at")
        self.assertIn("o.updated_at", s)


class TestBuildRegionFilter(unittest.TestCase):
    def test_all_returns_empty(self):
        for val in ["ALL", None, "all", ""]:
            self.assertEqual(oa.build_region_filter(val), ("", []))

    def test_specific_state(self):
        s, p = oa.build_region_filter("California")
        self.assertEqual(s, "AND s.state_name = %s")
        self.assertEqual(p, ["California"])


class TestBuildTypeFilter(unittest.TestCase):
    def test_all_returns_empty(self):
        self.assertEqual(oa.build_type_filter("ALL"), ("", []))
        self.assertEqual(oa.build_type_filter(None), ("", []))

    def test_for_profit(self):
        s, p = oa.build_type_filter("for_profit")
        self.assertEqual(p, ["For-profit"])

    def test_non_profit(self):
        s, p = oa.build_type_filter("non_profit")
        self.assertEqual(p, ["Non-Profit"])

    def test_unknown_falls_back_to_all(self):
        # Deliberate: unknown values don't crash the API — treated as ALL
        self.assertEqual(oa.build_type_filter("weird_type"), ("", []))


class TestResolveGroupBy(unittest.TestCase):
    def test_all_valid_values(self):
        for gb in ["daily", "weekly", "monthly", "yearly"]:
            unit, fmt = oa.resolve_group_by(gb)
            self.assertTrue(unit)
            self.assertTrue(fmt)

    def test_default_and_unknown_are_monthly(self):
        self.assertEqual(oa.resolve_group_by(None), oa.GROUPING["monthly"])
        self.assertEqual(oa.resolve_group_by("weird"), oa.GROUPING["monthly"])


# ── fetch helpers under test ───────────────────────────────────────────────

_SENTINEL = object()

def make_cursor(fetchall_returns=None, fetchone_return=_SENTINEL):
    cur = MagicMock()
    if fetchall_returns is not None:
        cur.fetchall.side_effect = fetchall_returns if isinstance(fetchall_returns, list) else [fetchall_returns]
    if fetchone_return is not _SENTINEL:
        cur.fetchone.return_value = fetchone_return
    return cur


BASELINE_FILTERS = {"date": ("", []), "region": ("", []), "type": ("", [])}


class TestFetchSummary(unittest.TestCase):
    def test_populated(self):
        cur = make_cursor(fetchone_return={
            "total_organizations": 126,
            "total_collaborators": 42,
            "total_contributors": 84,
            "average_org_rating": 4.2,
        })
        r = oa.fetch_summary(cur, BASELINE_FILTERS)
        self.assertEqual(r["total_organizations"], 126)
        self.assertEqual(r["average_org_rating"], 4.2)

    def test_empty(self):
        cur = make_cursor(fetchone_return={})
        r = oa.fetch_summary(cur, BASELINE_FILTERS)
        self.assertEqual(r, {
            "total_organizations": 0, "total_collaborators": 0,
            "total_contributors": 0, "average_org_rating": 0,
        })

    def test_null_row(self):
        cur = make_cursor(fetchone_return=None)
        self.assertEqual(oa.fetch_summary(cur, BASELINE_FILTERS)["total_organizations"], 0)


class TestFetchGrowthTrend(unittest.TestCase):
    def test_shape(self):
        cur = make_cursor(fetchall_returns=[[
            {"period": "2026-01", "total_organizations": 100, "total_collaborators": 34},
            {"period": "2026-02", "total_organizations": 108, "total_collaborators": 36},
        ]])
        r = oa.fetch_growth_trend(cur, BASELINE_FILTERS, "monthly")
        self.assertEqual(len(r), 2)
        self.assertEqual(r[0]["period"], "2026-01")
        self.assertEqual(r[1]["total_collaborators"], 36)

    def test_empty(self):
        cur = make_cursor(fetchall_returns=[[]])
        self.assertEqual(oa.fetch_growth_trend(cur, BASELINE_FILTERS, "daily"), [])


class TestFetchLocation(unittest.TestCase):
    def test_percentages(self):
        cur = make_cursor(fetchall_returns=[[
            {"state_id": "CA", "state_name": "California", "organization_count": 32, "percentage": 25.4},
            {"state_id": "TX", "state_name": "Texas", "organization_count": 24, "percentage": 19.0},
        ]])
        r = oa.fetch_organizations_by_location(cur, BASELINE_FILTERS)
        self.assertEqual(r[0]["state_id"], "CA")
        self.assertEqual(r[1]["percentage"], 19.0)

    def test_null_percentage_becomes_zero(self):
        cur = make_cursor(fetchall_returns=[[
            {"state_id": "XX", "state_name": None, "organization_count": 1, "percentage": None},
        ]])
        r = oa.fetch_organizations_by_location(cur, BASELINE_FILTERS)
        self.assertEqual(r[0]["percentage"], 0.0)


class TestFetchSize(unittest.TestCase):
    def test_shape(self):
        cur = make_cursor(fetchall_returns=[[
            {"org_size": "large", "organization_count": 31},
            {"org_size": "medium", "organization_count": 45},
            {"org_size": "small", "organization_count": 50},
        ]])
        r = oa.fetch_organizations_by_size(cur, BASELINE_FILTERS)
        self.assertEqual(len(r), 3)
        self.assertEqual(r[0]["org_size"], "large")


class TestCollabVsContrib(unittest.TestCase):
    def test_percentages(self):
        cur = make_cursor(fetchone_return={"collaborator_count": 42, "contributor_count": 84})
        r = oa.fetch_collaborator_vs_contributor(cur, BASELINE_FILTERS)
        self.assertEqual(r[0]["organization_count"], 42)
        self.assertEqual(r[0]["percentage"], 33.3)
        self.assertEqual(r[1]["percentage"], 66.7)

    def test_all_zero(self):
        cur = make_cursor(fetchone_return={"collaborator_count": 0, "contributor_count": 0})
        r = oa.fetch_collaborator_vs_contributor(cur, BASELINE_FILTERS)
        self.assertEqual(r[0]["percentage"], 0.0)
        self.assertEqual(r[1]["percentage"], 0.0)


class TestRatingDistribution(unittest.TestCase):
    def test_fills_missing_buckets(self):
        cur = make_cursor(fetchall_returns=[[
            {"rating": 4, "organization_count": 46},
            {"rating": 5, "organization_count": 64},
        ]])
        r = oa.fetch_rating_distribution(cur, BASELINE_FILTERS)
        self.assertEqual(len(r), 5)
        # 1/2/3 should be 0, 4=46, 5=64
        by_rating = {row["rating"]: row["organization_count"] for row in r}
        self.assertEqual(by_rating[1], 0)
        self.assertEqual(by_rating[3], 0)
        self.assertEqual(by_rating[4], 46)
        self.assertEqual(by_rating[5], 64)


class TestOrgTypeDistribution(unittest.TestCase):
    def test_shape(self):
        cur = make_cursor(fetchall_returns=[[
            {"period": "2026-01", "for_profit": 41, "non_profit": 68, "total": 109},
            {"period": "2026-02", "for_profit": 43, "non_profit": 68, "total": 111},
        ]])
        r = oa.fetch_organization_type_distribution(cur, BASELINE_FILTERS, "monthly")
        self.assertEqual(len(r), 2)
        self.assertEqual(r[0]["for_profit"], 41)
        self.assertEqual(r[0]["total"], 109)

    def test_null_counts_become_zero(self):
        cur = make_cursor(fetchall_returns=[[
            {"period": "2026-01", "for_profit": None, "non_profit": None, "total": None},
        ]])
        r = oa.fetch_organization_type_distribution(cur, BASELINE_FILTERS, "monthly")
        self.assertEqual(r[0]["for_profit"], 0)


# ── lambda_handler integration ─────────────────────────────────────────────

def _install_conn(mock_conn_fn, status_rows_seq, one_row=None):
    """Install a fake cursor: fetchall returns a queue of row lists, fetchone one row."""
    cur = MagicMock()
    cur.fetchall.side_effect = status_rows_seq
    cur.fetchone.return_value = one_row or {}
    conn = MagicMock()
    conn.cursor.return_value = cur
    mock_conn_fn.return_value = conn
    return conn, cur


@patch.dict(os.environ, MOCK_ENV)
class TestLambdaHandler(unittest.TestCase):
    """Full-handler integration. fetch_summary calls fetchone; fetch_collab_vs_contrib
    also calls fetchone. Everything else uses fetchall."""

    @patch("organization_analytics.get_db_connection")
    def test_default_event_returns_all_keys(self, mock_conn_fn):
        cur = MagicMock()
        cur.fetchone.side_effect = [
            {"total_organizations": 3, "total_collaborators": 1,
             "total_contributors": 2, "average_org_rating": 4.0},   # summary
            {"collaborator_count": 1, "contributor_count": 2},       # collab-vs-contrib
        ]
        cur.fetchall.side_effect = [
            [{"period": "2026-01", "total_organizations": 3, "total_collaborators": 1}],  # growth
            [{"state_id": "CA", "state_name": "California",
              "organization_count": 3, "percentage": 100.0}],                              # location
            [{"org_size": "small", "organization_count": 3}],                              # size
            [{"rating": 5, "organization_count": 3}],                                      # rating
            [{"period": "2026-01", "for_profit": 1, "non_profit": 2, "total": 3}],         # type
        ]
        conn = MagicMock()
        conn.cursor.return_value = cur
        mock_conn_fn.return_value = conn

        result = oa.lambda_handler({}, None)
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(set(body.keys()), REQUIRED_KEYS)
        self.assertEqual(body["summary"]["total_organizations"], 3)
        # rating distribution always contains 5 buckets
        self.assertEqual(len(body["rating_distribution"]), 5)

    @patch("organization_analytics.get_db_connection")
    def test_custom_range_threads_params(self, mock_conn_fn):
        cur = MagicMock()
        cur.fetchone.side_effect = [{}, {}]
        cur.fetchall.side_effect = [[], [], [], [], []]
        conn = MagicMock()
        conn.cursor.return_value = cur
        mock_conn_fn.return_value = conn

        oa.lambda_handler({
            "time_filter": "CUSTOM",
            "start_date": "2026-01-01",
            "end_date": "2026-06-30",
            "group_by": "monthly",
            "region": "ALL",
            "organization_type": "ALL",
        }, None)

        # Every fetch call should have received the two custom dates
        for call in cur.execute.call_args_list:
            _, params = call[0]
            self.assertIn("2026-01-01", params)
            self.assertIn("2026-06-30", params)

    @patch("organization_analytics.get_db_connection")
    def test_region_and_type_filters(self, mock_conn_fn):
        cur = MagicMock()
        cur.fetchone.side_effect = [{}, {}]
        cur.fetchall.side_effect = [[], [], [], [], []]
        conn = MagicMock()
        conn.cursor.return_value = cur
        mock_conn_fn.return_value = conn

        oa.lambda_handler({
            "time_filter": "1Y",
            "group_by": "monthly",
            "region": "California",
            "organization_type": "non_profit",
        }, None)

        for call in cur.execute.call_args_list:
            _, params = call[0]
            self.assertIn("California", params)
            self.assertIn("Non-Profit", params)

    @patch("organization_analytics.get_db_connection")
    def test_individual_query_failure_returns_safe_default(self, mock_conn_fn):
        cur = MagicMock()
        cur.execute.side_effect = Exception("boom")
        conn = MagicMock()
        conn.cursor.return_value = cur
        mock_conn_fn.return_value = conn

        result = oa.lambda_handler({}, None)
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        # All keys must still be present with safe defaults
        self.assertEqual(set(body.keys()), REQUIRED_KEYS)
        self.assertEqual(body["growth_trend"], [])
        self.assertEqual(body["summary"]["total_organizations"], 0)
        # rating_distribution should still have 5 buckets even on failure
        self.assertEqual(len(body["rating_distribution"]), 5)

    @patch("organization_analytics.get_db_connection", side_effect=Exception("no db"))
    def test_db_connection_failure_returns_500(self, _):
        result = oa.lambda_handler({}, None)
        self.assertEqual(result["statusCode"], 500)
        body = json.loads(result["body"])
        self.assertEqual(set(body.keys()), REQUIRED_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
