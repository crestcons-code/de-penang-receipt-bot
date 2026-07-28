import os
import json

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


def get_google_sheets_config():
    """
    Returns {"service_account_info": dict|None, "spreadsheet_id": str|None}.
    On Streamlit Cloud: reads st.secrets["gcp_service_account"] (a TOML table with
    the full service account JSON key fields) and st.secrets["GOOGLE_SHEETS_ID"].
    Locally: reads GOOGLE_SHEETS = {"service_account_json_path": "...", "spreadsheet_id": "..."}
    from config.py, loading the JSON key file from that path.
    Returns None values (not an exception) if not configured yet, so callers can
    show a helpful "not set up" message instead of crashing.
    """
    try:
        import streamlit as st
        if hasattr(st, "secrets") and "gcp_service_account" in st.secrets:
            return {
                "service_account_info": dict(st.secrets["gcp_service_account"]),
                "spreadsheet_id": st.secrets.get("GOOGLE_SHEETS_ID"),
            }
    except Exception:
        pass

    try:
        from config import GOOGLE_SHEETS
        path = GOOGLE_SHEETS.get("service_account_json_path")
        info = None
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
        return {
            "service_account_info": info,
            "spreadsheet_id": GOOGLE_SHEETS.get("spreadsheet_id"),
        }
    except Exception:
        return {"service_account_info": None, "spreadsheet_id": None}


_cfg = get_config()
AUTOCOUNT = _cfg["AUTOCOUNT"]
DEFAULT_PAYMENT_METHOD = _cfg["DEFAULT_PAYMENT_METHOD"]
MAYBANK_GL_CODE = _cfg["MAYBANK_GL_CODE"]
MATCH_THRESHOLD = _cfg["MATCH_THRESHOLD"]
