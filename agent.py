"""Chat with a Strands agent about the fabricated bus ridership database.

Setup:
    pip install strands-agents strands-agents-tools boto3
    export AWS_BEARER_TOKEN_BEDROCK="<your Bedrock API key>"
    python generate_data.py   # once, creates data/bus.db
    python agent.py
"""
import os
import sqlite3
from pathlib import Path

import boto3
from strands import Agent, tool
from strands.models import BedrockModel

DB_PATH = Path(__file__).parent / "data" / "bus.db"
MODEL_ID = "us.amazon.nova-lite-v1:0"
REGION = "us-east-1"
MAX_ROWS = 100

SYSTEM_PROMPT = """You are a data analyst for a city bus fare-tap dataset. All data is fabricated.
Workflow: call get_schema first, write a SQLite query, run it with run_sql, then answer with concrete numbers.
Prefer aggregates (COUNT, SUM, AVG, GROUP BY) over listing raw rows.
boarded_at is local time stored as text; use strftime / date / time functions for hour and weekday analysis.
If a query fails, read the error, fix the SQL and retry. Briefly state any assumptions you made."""


def _connect() -> sqlite3.Connection:
    # Read-only connection: the agent cannot modify the data even if it writes a bad statement.
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


@tool
def get_schema() -> str:
    """Return every table in the bus database with its columns and types. Call this before writing SQL."""
    conn = _connect()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        lines = []
        for table in tables:
            columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
            column_text = ", ".join(f"{c[1]} {c[2]}" for c in columns)
            lines.append(f"{table}({column_text})")
        return "\n".join(lines)
    finally:
        conn.close()


QUERY_LOG: list[dict] = []


def execute_query(query: str) -> dict:
    """Run a read-only query and return its columns, rows (capped) and any error."""
    conn = _connect()
    try:
        cursor = conn.execute(query)
        columns = [d[0] for d in cursor.description or []]
        if not columns:
            return {"sql": query, "columns": [], "rows": [], "truncated": False, "error": None}
        fetched = cursor.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as err:
        return {"sql": query, "columns": [], "rows": [], "truncated": False, "error": str(err)}
    finally:
        conn.close()
    return {"sql": query, "columns": columns, "rows": fetched[:MAX_ROWS],
            "truncated": len(fetched) > MAX_ROWS, "error": None}


@tool
def run_sql(query: str) -> str:
    """Run a read-only SQLite query on the bus database and return the result rows.

    Args:
        query: A single SELECT (or WITH ... SELECT) statement. Results are capped at 100 rows.
    """
    result = execute_query(query)
    QUERY_LOG.append(result)
    if result["error"]:
        return f"SQL error: {result['error']}. Fix the query and try again."
    if not result["columns"]:
        return "The query returned no result set."
    output = [" | ".join(result["columns"])] + [" | ".join(str(v) for v in row) for row in result["rows"]]
    if not result["rows"]:
        output.append("(no rows)")
    if result["truncated"]:
        output.append(f"(showing first {MAX_ROWS} rows; use GROUP BY or LIMIT to narrow the result)")
    return "\n".join(output)


model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
agent = Agent(model=model, tools=[get_schema, run_sql], system_prompt=SYSTEM_PROMPT,
              callback_handler=None)


def check_setup() -> str | None:
    """Return a message describing what is missing, or None if the chatbot can run."""
    if not DB_PATH.exists():
        return f"Database not found at {DB_PATH}. Run `python generate_data.py` first."
    if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK") and boto3.Session().get_credentials() is None:
        return ("No Bedrock credentials found. Run `export AWS_BEARER_TOKEN_BEDROCK=\"<your key>\"` "
                "in the same terminal, then start the app again.")
    return None


def ask(question: str) -> tuple[str, list[dict]]:
    """Ask the agent a question. Returns its answer and the SQL queries it ran."""
    QUERY_LOG.clear()
    result = agent(question)
    return str(result), list(QUERY_LOG)
