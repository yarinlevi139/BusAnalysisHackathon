"""Streamlit UI for the bus insights chatbot.

Run from this folder:
    streamlit run app.py
"""
import pandas as pd
import streamlit as st

from agent import MAX_ROWS, agent as chat_agent, ask, check_setup, execute_query, get_schema

st.set_page_config(page_title="Bus Insights", layout="wide")

setup_error = check_setup()
if setup_error:
    st.error(setup_error)
    st.stop()

if "messages" not in st.session_state:
    st.session_state.messages = []


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
    with st.expander(f"SQL queries ({len(queries)})", expanded=True):
        for number, query in enumerate(queries, start=1):
            st.markdown(f"**Query {number}**")
            st.code(query["sql"], language="sql")
            show_query_result(query, toggle_key=f"{key_prefix}-{number}")


with st.sidebar:
    st.header("Bus Insights")
    st.caption("Fabricated data. Ask questions in plain language.")
    with st.expander("Database schema"):
        st.code(get_schema(), language="text")
    if st.button("Clear chat"):
        st.session_state.messages = []
        chat_agent.messages.clear()
        st.rerun()

chat_tab, sql_tab = st.tabs(["Chat", "SQL console"])

with chat_tab:
    prompt = st.chat_input("Ask about the bus data")
    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt, "queries": []})
        try:
            with st.spinner("Thinking..."):
                answer, queries = ask(prompt)
            st.session_state.messages.append({"role": "assistant", "content": answer, "queries": queries})
        except Exception as err:
            st.session_state.messages.append({
                "role": "assistant",
                "content": f"Request failed: {type(err).__name__}: {err}",
                "queries": [],
            })

    for index, message in enumerate(st.session_state.messages):
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message["queries"]:
                show_queries(message["queries"], key_prefix=f"msg{index}")

with sql_tab:
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
