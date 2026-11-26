"""Chat with a Strands agent about the fabricated bus ridership database.

Setup:
    pip install strands-agents strands-agents-tools boto3
    export AWS_BEARER_TOKEN_BEDROCK="<your Bedrock API key>"
    python generate_data.py   # once, creates data/bus.db
    streamlit run app.py
"""
import os
import queue
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import boto3
from strands import Agent, tool
from strands.models import BedrockModel

DB_PATH = Path(__file__).parent / "data" / "bus.db"
MODEL_ID = "us.amazon.nova-lite-v1:0"
REGION = "us-east-1"
MAX_ROWS = 100

SYSTEM_PROMPT = """You are Bus Insights, a friendly assistant for a city bus fare-tap dataset (all data is fabricated).

Talk with the user naturally, like a helpful colleague in a chat.
- For greetings, small talk, thanks, or questions about what you can do, just reply conversationally. Do not call tools.
- Only use tools when answering requires actual data from the database.
- When you need data: call get_schema if you do not already know the tables from this conversation, write a SQLite
  query, run it with run_sql, then answer with the concrete numbers.
- Prefer aggregates (COUNT, SUM, AVG, GROUP BY) over listing raw rows.
- boarded_at is local time stored as text; use strftime / date / time functions for hour and weekday analysis.
- If a query fails, read the error, fix the SQL and retry.
- Keep answers concise and readable. Use short paragraphs, bullet points or small markdown tables. Do not paste SQL
  into the answer unless the user asks for it. Briefly mention any assumptions you made."""


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


_event_queue: queue.Queue | None = None


def _forward_events(**kwargs) -> None:
    """Strands callback: pass streamed text and tool starts to whoever is consuming ask_stream()."""
    if _event_queue is None:
        return
    if kwargs.get("data"):
        _event_queue.put(("text", kwargs["data"]))
    tool_use = kwargs.get("event", {}).get("contentBlockStart", {}).get("start", {}).get("toolUse")
    if tool_use:
        _event_queue.put(("tool", tool_use["name"]))


model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
agent = Agent(model=model, tools=[get_schema, run_sql], system_prompt=SYSTEM_PROMPT,
              callback_handler=_forward_events)


class _TagFilter:
    """Removes Nova's <thinking>...</thinking> blocks and <response> tags from streamed text."""

    TAGS = ("<thinking>", "</thinking>", "<response>", "</response>")

    def __init__(self) -> None:
        self._buffer = ""
        self._in_thinking = False

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        out = []
        while self._buffer:
            if self._in_thinking:
                end = self._buffer.find("</thinking>")
                if end == -1:
                    # Keep a possible partial closing tag for the next chunk.
                    self._buffer = self._buffer[-(len("</thinking>") - 1):]
                    break
                self._buffer = self._buffer[end + len("</thinking>"):]
                self._in_thinking = False
                continue
            start = self._buffer.find("<")
            if start == -1:
                out.append(self._buffer)
                self._buffer = ""
                break
            out.append(self._buffer[:start])
            rest = self._buffer[start:]
            tag = next((t for t in self.TAGS if rest.startswith(t)), None)
            if tag:
                self._in_thinking = tag == "<thinking>"
                self._buffer = rest[len(tag):]
            elif any(t.startswith(rest) for t in self.TAGS):
                self._buffer = rest
                break
            else:
                out.append("<")
                self._buffer = rest[1:]
        return "".join(out)

    def flush(self) -> str:
        rest, self._buffer = ("" if self._in_thinking else self._buffer), ""
        return rest


def check_setup() -> str | None:
    """Return a message describing what is missing, or None if the chatbot can run."""
    if not DB_PATH.exists():
        return f"Database not found at {DB_PATH}. Run `python generate_data.py` first."
    if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK") and boto3.Session().get_credentials() is None:
        return ("No Bedrock credentials found. Run `export AWS_BEARER_TOKEN_BEDROCK=\"<your key>\"` "
                "in the same terminal, then start the app again.")
    return None


FOLLOW_UP = "Using the query results above, state the final answer in plain English with the concrete numbers."


def _stream_turn(prompt: str) -> Iterator[tuple[str, str]]:
    """Run one agent call in a background thread, yielding ("text", chunk) and ("tool", name) events."""
    global _event_queue
    events: queue.Queue = queue.Queue()
    errors: list[BaseException] = []
    done = object()

    def run() -> None:
        try:
            agent(prompt)
        except BaseException as err:  # re-raised in the consumer's thread
            errors.append(err)
        finally:
            events.put(done)

    _event_queue = events
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    tag_filter = _TagFilter()
    pending_break = False
    has_text = False
    try:
        while (event := events.get()) is not done:
            kind, value = event
            if kind == "tool":
                pending_break = has_text
                yield event
                continue
            text = tag_filter.feed(value)
            if not has_text:
                text = text.lstrip()
            if text:
                if pending_break:
                    # Separate text written before and after a tool call.
                    text = "\n\n" + text.lstrip()
                    pending_break = False
                has_text = True
                yield ("text", text)
        tail = tag_filter.flush().rstrip()
        if tail:
            yield ("text", tail)
    finally:
        _event_queue = None
        thread.join()
    if errors:
        raise errors[0]


def ask_stream(question: str) -> Iterator[tuple[str, str]]:
    """Stream the agent's reply as ("text", chunk) and ("tool", name) events.

    The SQL queries run during the reply are available in QUERY_LOG afterwards.
    """
    QUERY_LOG.clear()
    has_text = False
    for event in _stream_turn(question):
        has_text = has_text or (event[0] == "text" and bool(event[1].strip()))
        yield event
    if not has_text and QUERY_LOG:
        # The model ran SQL but ended its turn without text, so ask it to summarise the results.
        for event in _stream_turn(FOLLOW_UP):
            has_text = has_text or (event[0] == "text" and bool(event[1].strip()))
            yield event
    if not has_text:
        yield ("text", "The model returned no answer. Try asking the question again.")


def ask(question: str) -> tuple[str, list[dict]]:
    """Ask the agent a question. Returns its answer and the SQL queries it ran."""
    answer = "".join(value for kind, value in ask_stream(question) if kind == "text")
    return answer.strip(), list(QUERY_LOG)
