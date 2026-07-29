# google_sheets_client.py - Google Sheets integration for the dana list workflow
#
# Two jobs:
#   1. Push a parsed bank statement into a new tab of the dana list Google Sheet,
#      in the same 12-column layout volunteers already use, ready for them to fill
#      in donor details as WhatsApp messages come in.
#   2. Read the live (in-progress or finished) sheet back as a DataFrame, in the
#      same layout the Excel upload produces, so it can reuse the exact same
#      parsing logic (_parse_dana_dataframe in app.py).

import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Must match the dana list Excel column order exactly (12 columns)
DANA_LIST_HEADERS = [
    "Transaction Date",
    "Transaction Description 1",
    "Transaction Description 2",
    "Beneficiary/ Biller Name",
    "MBB Receiving/Paying Account",
    "Transaction Amount: Cash-in (RM)",
    "Receipts No",
    "accounting code",
    "Dana description \nTo follow Donor's WA",
    "Donor name \n(Indicated on Receipt)",
    "Whatspps name",
    "Whatsapps Mobile",
]


def get_client(service_account_info: dict) -> gspread.Client:
    """service_account_info: the parsed JSON key dict (from Streamlit secrets or a local file)."""
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    return gspread.authorize(creds)


def push_bank_statement(client: gspread.Client, spreadsheet_id: str, tab_name: str,
                        df_bank: pd.DataFrame, overwrite: bool = False) -> str:
    """
    Create (or clear, if overwrite=True and it exists) a worksheet tab named tab_name
    and write the parsed bank statement into it using the dana list column layout.
    Columns A-F (date, desc1, desc2, beneficiary, account, amount) are filled in;
    columns G-L (Receipts No onward) are left blank for volunteers to fill in.
    df_bank: output of parse_maybank.load_statement() - has columns
             date, donor_name, description, gl_text, credit, source_file (CSV),
             or date, description, credit, source_file (PDF).
    Returns the worksheet URL.
    """
    sh = client.open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet(tab_name)
        if overwrite:
            ws.clear()
        else:
            raise ValueError(f"Sheet tab '{tab_name}' already exists. Pass overwrite=True to replace it.")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=len(df_bank) + 10, cols=len(DANA_LIST_HEADERS))

    rows = [DANA_LIST_HEADERS]
    for _, r in df_bank.iterrows():
        date_str = r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"])
        desc1 = str(r.get("description", "") or r.get("gl_text", ""))
        beneficiary = str(r.get("donor_name", ""))
        amount = float(r["credit"])
        rows.append([date_str, desc1, "", beneficiary, "", amount, "", "", "", "", "", ""])

    ws.update(rows, value_input_option="USER_ENTERED")
    return ws.url


def read_dana_list(client: gspread.Client, spreadsheet_id: str, tab_name: str) -> pd.DataFrame:
    """
    Read a worksheet tab back as a DataFrame matching the dana list Excel layout,
    ready to pass into app.py's _parse_dana_dataframe().

    Reads by COLUMN POSITION rather than requiring exact header text - real sheets
    often have extra leading/trailing spaces or minor wording differences in the
    header row, and _parse_dana_dataframe only relies on column order anyway
    (same as how the Excel upload path works).
    """
    import numpy as np
    sh = client.open_by_key(spreadsheet_id)
    ws = sh.worksheet(tab_name)
    values = ws.get_all_values()
    if len(values) < 2:
        return pd.DataFrame(columns=DANA_LIST_HEADERS)

    header = [str(h).strip() for h in values[0]]
    data = values[1:]
    # get_all_values() pads/truncates every row to the header's width already
    df = pd.DataFrame(data, columns=header)
    # Blank cells come back as "" from Sheets - treat them as missing, same as
    # how pandas reads genuinely empty Excel cells, so pd.isna() checks work
    df = df.replace(r"^\s*$", np.nan, regex=True)
    # Track each row's actual sheet row number (1=header, so first data row is 2)
    # so a caller can write results (like the OR number after posting) back to
    # the exact right row later.
    df["_sheet_row"] = range(2, len(df) + 2)
    return df


def write_or_numbers(client: gspread.Client, spreadsheet_id: str, tab_name: str,
                     row_or_map: dict) -> None:
    """
    Write OR numbers back into column G (Receipts No) for specific rows.
    row_or_map: {sheet_row_number: "OR-2607123", ...}
    One batched API call regardless of how many rows are updated.
    """
    if not row_or_map:
        return
    sh = client.open_by_key(spreadsheet_id)
    ws = sh.worksheet(tab_name)
    updates = [{"range": f"G{row}", "values": [[or_no]]} for row, or_no in row_or_map.items()]
    ws.batch_update(updates, value_input_option="USER_ENTERED")


def list_tabs(client: gspread.Client, spreadsheet_id: str) -> list:
    """Return the names of all worksheet tabs in the spreadsheet, for a tab picker."""
    sh = client.open_by_key(spreadsheet_id)
    return [ws.title for ws in sh.worksheets()]
