"""Local Streamlit entry point for the five-page Personal AI OS."""

import streamlit as st
from personal_ai_os.ui.styles import apply_style

st.set_page_config(page_title="Personal AI OS", page_icon="🌿", layout="wide")
apply_style()
st.sidebar.markdown("### 🌿 Personal AI OS")
st.sidebar.caption("学习与生活，一处管理")
pages = [
    st.Page("pages/01_today.py", title="对话与今日计划", icon="🌤️", url_path="today", default=True),
    st.Page("pages/02_tasks.py", title="任务与日程", icon="✅", url_path="tasks"),
    st.Page("pages/03_agents.py", title="Agent 控制台", icon="🤖", url_path="agents"),
    st.Page("pages/04_trace.py", title="执行轨迹", icon="🧭", url_path="trace"),
    st.Page("pages/05_memory.py", title="记忆与设置", icon="⚙️", url_path="memory"),
]
st.navigation(pages).run()
