"""Local Streamlit entry point for the five-page Personal AI OS."""

import streamlit as st

st.set_page_config(page_title="Personal AI OS", layout="wide")
pages = [
    st.Page("pages/01_today.py", title="对话与今日计划", url_path="today", default=True),
    st.Page("pages/02_tasks.py", title="任务与日程", url_path="tasks"),
    st.Page("pages/03_agents.py", title="Agent 控制台", url_path="agents"),
    st.Page("pages/04_trace.py", title="执行轨迹", url_path="trace"),
    st.Page("pages/05_memory.py", title="记忆与设置", url_path="memory"),
]
st.navigation(pages).run()
