"""Shared presentation for the five local Streamlit pages."""

import streamlit as st


def apply_style() -> None:
    st.markdown(
        """<style>
        :root { color-scheme: light; }
        [data-testid="stAppViewContainer"] {
            background: #f6f8f7;
            color: #19302e;
        }
        [data-testid="stSidebar"] {
            background: #edf3f1;
            border-right: 1px solid #dce7e3;
        }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
            color: #526b66;
        }
        [data-testid="stMainBlockContainer"] {
            max-width: 1180px;
            padding-top: 2.2rem;
            padding-bottom: 5rem;
        }
        h1, h2, h3 { color: #19302e; letter-spacing: -0.025em; }
        h1 { font-size: clamp(2rem, 3vw, 2.75rem) !important; font-weight: 750 !important; }
        h3 { margin-top: 1.2rem !important; }
        [data-testid="stCaptionContainer"] { color: #5d746f; }
        [data-testid="stMetric"] {
            background: #fff;
            border: 1px solid #dce8e3;
            border-radius: 16px;
            padding: 1rem 1.2rem;
            box-shadow: 0 5px 18px rgba(27, 69, 61, 0.035);
        }
        [data-testid="stMetricLabel"] { color: #526b66; }
        [data-testid="stMetricValue"] { color: #176b62; font-weight: 750; }
        [data-testid="stForm"], [data-testid="stExpander"] {
            background: #fff;
            border: 1px solid #dce8e3;
            border-radius: 14px;
        }
        [data-testid="stTabs"] [role="tablist"] {
            gap: .35rem;
            border-bottom: 1px solid #dce8e3;
            margin-bottom: 1.1rem;
        }
        [data-testid="stTabs"] [role="tab"] {
            border-radius: 10px 10px 0 0;
            padding: .65rem 1rem;
            font-weight: 600;
        }
        [data-testid="stTabs"] [role="tab"][aria-selected="true"] {
            color: #176b62;
            background: #e5f1ed;
        }
        [data-baseweb="tab-highlight"] { background-color: #176b62 !important; }
        [data-testid="stButton"] button,
        [data-testid="stFormSubmitButton"] button {
            border-radius: 10px;
            min-height: 2.5rem;
            font-weight: 600;
        }
        [data-testid="stButton"] button[kind="primary"],
        [data-testid="stFormSubmitButton"] button[kind="primary"] {
            background: #176b62;
            border-color: #176b62;
        }
        [data-testid="stDataFrame"] {
            border: 1px solid #dce8e3;
            border-radius: 12px;
            overflow: hidden;
        }
        @media (max-width: 760px) {
            [data-testid="stMainBlockContainer"] { padding: 1.2rem .8rem 3rem; }
            [data-testid="stTabs"] [role="tab"] { padding: .55rem .65rem; }
        }
        </style>""",
        unsafe_allow_html=True,
    )
