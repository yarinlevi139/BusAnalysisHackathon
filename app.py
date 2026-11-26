"""Streamlit UI for the bus insights chatbot.

Run from this folder:
    streamlit run app.py
"""
import pandas as pd
import streamlit as st

from agent import MAX_ROWS, QUERY_LOG, agent as chat_agent, ask_stream, check_setup, execute_query, get_schema

st.set_page_config(page_title="Bus Insights", page_icon="🚌", layout="centered")

st.markdown(
    """
    <style>
    .block-container { padding-top: 2rem; padding-bottom: 6rem; max-width: 820px; }
    .welcome { text-align: center; margin-top: 8vh; margin-bottom: 2rem; }
    .welcome h1 { font-size: 2rem; font-weight: 600; }
    .welcome p { color: #8a8a8a; }
    </style>
    """,
    unsafe_allow_html=True,
)

setup_error = check_setup()
if setup_error:
    st.error(setup_error)
    st.stop()

if "messages" not in st.session_state:
    st.session_state.messages = []

SUGGESTIONS = [
    "Which stop has the most boardings on weekday mornings?",
    "How much do rainy days change ridership?",
    "What share of fares are free transfers, by rider type?",
    "What can you help me with?",
]
TOOL_LABELS = {"get_schema": "Looking at the database schema...", "run_sql": "Querying the database..."}


def show_query_result(query: dict, toggle_key: str) -> None:
    if query["error"]:
        st.error(query["error"])
        return
    if not query["columns"]:
        st.caption("No result set.")
        return
    if st.toggle("Show data", key=toggle_key):
        st.dataframe(pd.DataFrame(query["rows"], columns=query["columns"]), hide_index=True)
        if query["truncated"]:
            st.caption(f"Showing the first {MAX_ROWS} rows.")
        else:
            st.caption(f"{len(query['rows'])} rows")


def show_queries(queries: list[dict], key_prefix: str) -> None:
    label = "Show SQL" if len(queries) == 1 else f"Show SQL ({len(queries)} queries)"
    with st.expander(label, expanded=False):
        for number, query in enumerate(queries, start=1):
            if len(queries) > 1:
                st.caption(f"Query {number}")
            st.code(query["sql"], language="sql")
            show_query_result(query, toggle_key=f"{key_prefix}-{number}")


def stream_reply(prompt: str) -> tuple[str, list[dict]]:
    """Show the assistant's reply as it is generated. Returns the final text and the SQL it ran."""
    status = st.empty()
    body = st.empty()
    status.caption("Thinking...")
    text = ""
    try:
        for kind, value in ask_stream(prompt):
            if kind == "tool":
                status.caption(TOOL_LABELS.get(value, f"Using {value}..."))
            else:
                text += value
                body.markdown(text + " ▌")
    except Exception as err:
        text += f"\n\nRequest failed: {type(err).__name__}: {err}"
    status.empty()
    body.markdown(text)
    return text.strip(), list(QUERY_LOG)


def chat_view() -> None:
    pending = st.session_state.pop("pending_prompt", None)
    prompt = st.chat_input("Ask anything about the bus data") or pending

    if not st.session_state.messages and not prompt:
        st.markdown(
            '<div class="welcome"><h1>🚌 Bus Insights</h1>'
            "<p>Ask me anything about the bus ridership data.</p></div>",
            unsafe_allow_html=True,
        )
        columns = st.columns(2)
        for index, suggestion in enumerate(SUGGESTIONS):
            if columns[index % 2].button(suggestion, key=f"suggestion-{index}", width="stretch"):
                st.session_state.pending_prompt = suggestion
                st.rerun()
        return

    for index, message in enumerate(st.session_state.messages):
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message["queries"]:
                show_queries(message["queries"], key_prefix=f"msg{index}")

    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt, "queries": []})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            answer, queries = stream_reply(prompt)
            if queries:
                show_queries(queries, key_prefix=f"msg{len(st.session_state.messages)}")
        st.session_state.messages.append({"role": "assistant", "content": answer, "queries": queries})


def sql_view() -> None:
    st.subheader("SQL console")
    st.caption("Run read-only SQLite queries directly against the database.")
    sql_text = st.text_area(
        "SQL",
        value="SELECT line_number, COUNT(*) AS boardings\nFROM boardings\nGROUP BY line_number\nORDER BY boardings DESC;",
        height=160,
    )
    if st.button("Run query", type="primary"):
        result = execute_query(sql_text)
        if result["error"]:
            st.error(result["error"])
        elif not result["columns"]:
            st.info("The query returned no result set.")
        else:
            st.dataframe(pd.DataFrame(result["rows"], columns=result["columns"]), hide_index=True)
            if result["truncated"]:
                st.caption(f"Showing the first {MAX_ROWS} rows.")


with st.sidebar:
    st.header("🚌 Bus Insights")
    if st.button("➕ New chat", width="stretch"):
        st.session_state.messages = []
        chat_agent.messages.clear()
        st.rerun()
    view = st.radio("View", ["Chat", "SQL console"], label_visibility="collapsed")
    st.caption("Fabricated data. Ask questions in plain language.")
    with st.expander("Database schema"):
        st.code(get_schema(), language="text")

if view == "Chat":
    chat_view()
else:
    sql_view()
