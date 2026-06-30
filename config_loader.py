import os

def get_config():
    """Load config from Streamlit secrets (cloud) or local config.py (local)."""
    try:
        import streamlit as st
        if hasattr(st, "secrets") and "AUTOCOUNT" in st.secrets:
            return {
                "AUTOCOUNT": dict(st.secrets["AUTOCOUNT"]),
                "DEFAULT_PAYMENT_METHOD": st.secrets.get("DEFAULT_PAYMENT_METHOD", "BANK"),
                "MAYBANK_GL_CODE": st.secrets.get("MAYBANK_GL_CODE", "310-1000"),
                "MATCH_THRESHOLD": int(st.secrets.get("MATCH_THRESHOLD", 75)),
            }
    except Exception:
        pass

    # Fall back to local config.py
    from config import AUTOCOUNT, DEFAULT_PAYMENT_METHOD, MAYBANK_GL_CODE, MATCH_THRESHOLD
    return {
        "AUTOCOUNT": AUTOCOUNT,
        "DEFAULT_PAYMENT_METHOD": DEFAULT_PAYMENT_METHOD,
        "MAYBANK_GL_CODE": MAYBANK_GL_CODE,
        "MATCH_THRESHOLD": MATCH_THRESHOLD,
    }

_cfg = get_config()
AUTOCOUNT = _cfg["AUTOCOUNT"]
DEFAULT_PAYMENT_METHOD = _cfg["DEFAULT_PAYMENT_METHOD"]
MAYBANK_GL_CODE = _cfg["MAYBANK_GL_CODE"]
MATCH_THRESHOLD = _cfg["MATCH_THRESHOLD"]
