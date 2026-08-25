"""
Organization Analytics API — Issue #228

Powers the Organization Dashboard: summary KPIs, growth trend, geographic
distribution, size / collaborator-vs-contributor / rating / type breakdowns.

DB credentials come from environment variables (DB_HOST, DB_NAME, DB_USER,
DB_PASSWORD, DB_PORT) — NO AWS Parameter Store, per issue instructions.
"""

import json
import os

import psycopg2
from psycopg2.extras import RealDictCursor


SCHEMA_NAME = "virginia_dev_saayam_rdbms"
ORG_TABLE = f"{SCHEMA_NAME}.organizations"
STATE_TABLE = f"{SCHEMA_NAME}.state"

# Allowed group_by values → PostgreSQL DATE_TRUNC unit + display format
GROUPING = {
    "daily":   ("day",   "YYYY-MM-DD"),
    "weekly":  ("week",  "IYYY-\"W\"IW"),
    "monthly": ("month", "YYYY-MM"),
    "yearly":  ("year",  "YYYY"),
}

# Frontend org_type values → what's stored in the DB (case + hyphen quirks)
ORG_TYPE_MAP = {
    "for_profit": "For-profit",
    "non_profit": "Non-Profit",
}


def build_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }


def get_default_response():
    return {
        "summary": {
            "total_organizations": 0,
            "total_collaborators": 0,
            "total_contributors": 0,
            "average_org_rating": 0,
        },
        "growth_trend": [],
        "organizations_by_location": [],
        "organizations_by_size": [],
        "collaborator_vs_contributor": [],
        "rating_distribution": [],
        "organization_type_distribution": [],
    }


def get_db_connection():
    """Local PostgreSQL connection. Env vars only — no SSM, per issue #228."""
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        port=int(os.environ.get("DB_PORT", 5432)),
    )


# ── filter builders ────────────────────────────────────────────────────────

def build_date_filter(time_filter, start_date=None, end_date=None, column="o.created_at"):
    """Return (sql_snippet, params) that filters `column` by the given range.

    Snippet is either empty (no filter) or prefixed with 'AND '.
    """
    tf = (time_filter or "ALL").strip().upper()
    if tf == "7D":
        return (f"AND {column} > CURRENT_TIMESTAMP - INTERVAL '7 days'", [])
    if tf == "30D":
        return (f"AND {column} > CURRENT_TIMESTAMP - INTERVAL '30 days'", [])
    if tf == "1Y":
        return (f"AND {column} > CURRENT_TIMESTAMP - INTERVAL '1 year'", [])
    if tf == "CUSTOM" and start_date and end_date:
        return (f"AND {column} BETWEEN %s AND %s", [start_date, end_date])
    return ("", [])  # ALL / unknown / incomplete CUSTOM


def build_region_filter(region):
    """Filter by state name (matches state.state_name). 'ALL'/None = no filter."""
    if not region or str(region).strip().upper() == "ALL":
        return ("", [])
    return ("AND s.state_name = %s", [region])


def build_type_filter(org_type):
    """Filter by organization type. Accepts 'for_profit'/'non_profit'. 'ALL'/None = no filter."""
    if not org_type or str(org_type).strip().upper() == "ALL":
        return ("", [])
    mapped = ORG_TYPE_MAP.get(str(org_type).strip().lower())
    if mapped is None:
        return ("", [])  # unknown value → treat as ALL rather than crash
    return ("AND o.org_type = %s", [mapped])


def resolve_group_by(group_by):
    """Return (trunc_unit, char_format) for growth-trend grouping."""
    return GROUPING.get((group_by or "monthly").strip().lower(), GROUPING["monthly"])


# ── fetch functions ────────────────────────────────────────────────────────

def fetch_summary(cursor, filters):
    df, dp = filters["date"]
    rf, rp = filters["region"]
    tf, tp = filters["type"]
    query = f"""
        SELECT
            COUNT(*) AS total_organizations,
            COALESCE(SUM(CASE WHEN o.is_collaborator THEN 1 ELSE 0 END), 0) AS total_collaborators,
            COALESCE(SUM(CASE WHEN o.is_contributor  THEN 1 ELSE 0 END), 0) AS total_contributors,
            ROUND(AVG(o.org_rating)::numeric, 2) AS average_org_rating
        FROM {ORG_TABLE} o
        LEFT JOIN {STATE_TABLE} s ON o.state_id = s.state_id
        WHERE 1=1 {df} {rf} {tf};
    """
    cursor.execute(query, dp + rp + tp)
    row = cursor.fetchone() or {}
    return {
        "total_organizations": int(row.get("total_organizations") or 0),
        "total_collaborators": int(row.get("total_collaborators") or 0),
        "total_contributors": int(row.get("total_contributors") or 0),
        "average_org_rating": float(row.get("average_org_rating") or 0),
    }


def fetch_growth_trend(cursor, filters, group_by):
    unit, fmt = resolve_group_by(group_by)
    df, dp = filters["date"]
    rf, rp = filters["region"]
    tf, tp = filters["type"]
    query = f"""
        SELECT
            TO_CHAR(DATE_TRUNC('{unit}', o.created_at), '{fmt}') AS period,
            COUNT(*) AS total_organizations,
            COALESCE(SUM(CASE WHEN o.is_collaborator THEN 1 ELSE 0 END), 0) AS total_collaborators
        FROM {ORG_TABLE} o
        LEFT JOIN {STATE_TABLE} s ON o.state_id = s.state_id
        WHERE o.created_at IS NOT NULL {df} {rf} {tf}
        GROUP BY 1
        ORDER BY 1;
    """
    cursor.execute(query, dp + rp + tp)
    return [
        {
            "period": row["period"],
            "total_organizations": int(row["total_organizations"]),
            "total_collaborators": int(row["total_collaborators"]),
        }
        for row in cursor.fetchall()
    ]


def fetch_organizations_by_location(cursor, filters):
    df, dp = filters["date"]
    rf, rp = filters["region"]
    tf, tp = filters["type"]
    query = f"""
        WITH loc AS (
            SELECT
                o.state_id,
                s.state_name,
                COUNT(*) AS organization_count
            FROM {ORG_TABLE} o
            LEFT JOIN {STATE_TABLE} s ON o.state_id = s.state_id
            WHERE 1=1 {df} {rf} {tf}
            GROUP BY o.state_id, s.state_name
        )
        SELECT
            state_id,
            state_name,
            organization_count,
            ROUND(100.0 * organization_count / NULLIF(SUM(organization_count) OVER (), 0), 1) AS percentage
        FROM loc
        ORDER BY organization_count DESC, state_id;
    """
    cursor.execute(query, dp + rp + tp)
    return [
        {
            "state_id": row["state_id"],
            "state_name": row["state_name"],
            "organization_count": int(row["organization_count"]),
            "percentage": float(row["percentage"]) if row["percentage"] is not None else 0.0,
        }
        for row in cursor.fetchall()
    ]


def fetch_organizations_by_size(cursor, filters):
    df, dp = filters["date"]
    rf, rp = filters["region"]
    tf, tp = filters["type"]
    query = f"""
        SELECT
            LOWER(o.org_size) AS org_size,
            COUNT(*) AS organization_count
        FROM {ORG_TABLE} o
        LEFT JOIN {STATE_TABLE} s ON o.state_id = s.state_id
        WHERE o.org_size IS NOT NULL {df} {rf} {tf}
        GROUP BY LOWER(o.org_size)
        ORDER BY org_size;
    """
    cursor.execute(query, dp + rp + tp)
    return [
        {"org_size": row["org_size"], "organization_count": int(row["organization_count"])}
        for row in cursor.fetchall()
    ]


def fetch_collaborator_vs_contributor(cursor, filters):
    df, dp = filters["date"]
    rf, rp = filters["region"]
    tf, tp = filters["type"]
    query = f"""
        SELECT
            COALESCE(SUM(CASE WHEN o.is_collaborator THEN 1 ELSE 0 END), 0) AS collaborator_count,
            COALESCE(SUM(CASE WHEN o.is_contributor  THEN 1 ELSE 0 END), 0) AS contributor_count
        FROM {ORG_TABLE} o
        LEFT JOIN {STATE_TABLE} s ON o.state_id = s.state_id
        WHERE 1=1 {df} {rf} {tf};
    """
    cursor.execute(query, dp + rp + tp)
    row = cursor.fetchone() or {}
    collab = int(row.get("collaborator_count") or 0)
    contrib = int(row.get("contributor_count") or 0)
    total = collab + contrib
    def pct(x): return round(100.0 * x / total, 1) if total else 0.0
    return [
        {"type": "collaborator", "organization_count": collab, "percentage": pct(collab)},
        {"type": "contributor",  "organization_count": contrib, "percentage": pct(contrib)},
    ]


def fetch_rating_distribution(cursor, filters):
    df, dp = filters["date"]
    rf, rp = filters["region"]
    tf, tp = filters["type"]
    query = f"""
        SELECT o.org_rating AS rating, COUNT(*) AS organization_count
        FROM {ORG_TABLE} o
        LEFT JOIN {STATE_TABLE} s ON o.state_id = s.state_id
        WHERE o.org_rating IS NOT NULL {df} {rf} {tf}
        GROUP BY o.org_rating
        ORDER BY o.org_rating;
    """
    cursor.execute(query, dp + rp + tp)
    seen = {int(row["rating"]): int(row["organization_count"]) for row in cursor.fetchall()}
    # Always return all 5 buckets so the frontend can render a stable chart
    return [{"rating": r, "organization_count": seen.get(r, 0)} for r in range(1, 6)]


def fetch_organization_type_distribution(cursor, filters, group_by):
    unit, fmt = resolve_group_by(group_by)
    df, dp = filters["date"]
    rf, rp = filters["region"]
    tf, tp = filters["type"]
    query = f"""
        SELECT
            TO_CHAR(DATE_TRUNC('{unit}', o.created_at), '{fmt}') AS period,
            SUM(CASE WHEN o.org_type = 'For-profit' THEN 1 ELSE 0 END) AS for_profit,
            SUM(CASE WHEN o.org_type = 'Non-Profit' THEN 1 ELSE 0 END) AS non_profit,
            COUNT(*) AS total
        FROM {ORG_TABLE} o
        LEFT JOIN {STATE_TABLE} s ON o.state_id = s.state_id
        WHERE o.created_at IS NOT NULL {df} {rf} {tf}
        GROUP BY 1
        ORDER BY 1;
    """
    cursor.execute(query, dp + rp + tp)
    return [
        {
            "period": row["period"],
            "for_profit": int(row["for_profit"] or 0),
            "non_profit": int(row["non_profit"] or 0),
            "total": int(row["total"] or 0),
        }
        for row in cursor.fetchall()
    ]


# ── entry point ────────────────────────────────────────────────────────────

def _run_safely(label, response_body, key, fn, default):
    """Call fn and drop result into response_body[key]; log and use default on error."""
    try:
        response_body[key] = fn()
    except Exception as error:
        print(f"{label} query failed: {error}")
        response_body[key] = default


def lambda_handler(event, context):
    event = event or {}
    time_filter = event.get("time_filter", "ALL")
    start_date = event.get("start_date")
    end_date = event.get("end_date")
    group_by = event.get("group_by", "monthly")
    region = event.get("region", "ALL")
    org_type = event.get("organization_type", "ALL")

    filters = {
        "date": build_date_filter(time_filter, start_date, end_date),
        "region": build_region_filter(region),
        "type": build_type_filter(org_type),
    }

    response_body = get_default_response()
    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        _run_safely("summary", response_body, "summary",
                    lambda: fetch_summary(cursor, filters),
                    response_body["summary"])
        _run_safely("growth_trend", response_body, "growth_trend",
                    lambda: fetch_growth_trend(cursor, filters, group_by), [])
        _run_safely("organizations_by_location", response_body, "organizations_by_location",
                    lambda: fetch_organizations_by_location(cursor, filters), [])
        _run_safely("organizations_by_size", response_body, "organizations_by_size",
                    lambda: fetch_organizations_by_size(cursor, filters), [])
        _run_safely("collaborator_vs_contributor", response_body, "collaborator_vs_contributor",
                    lambda: fetch_collaborator_vs_contributor(cursor, filters), [])
        _run_safely("rating_distribution", response_body, "rating_distribution",
                    lambda: fetch_rating_distribution(cursor, filters),
                    [{"rating": r, "organization_count": 0} for r in range(1, 6)])
        _run_safely("organization_type_distribution", response_body, "organization_type_distribution",
                    lambda: fetch_organization_type_distribution(cursor, filters, group_by), [])

        return build_response(200, response_body)

    except Exception as error:
        print(f"DB connection failed: {error}")
        return build_response(500, response_body)

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


if __name__ == "__main__":
    # ponytail: local smoke — needs DB_* env vars pointing to a local postgres
    # populated from data-analytics/sql/organizations.csv + state.csv
    result = lambda_handler(
        {"time_filter": "30D", "group_by": "daily", "region": "ALL", "organization_type": "ALL"},
        None,
    )
    print(json.dumps(json.loads(result["body"]), indent=2))
