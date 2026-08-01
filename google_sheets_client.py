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
                     row_or_map: dict) -> dict:
    """
    Write OR numbers back into column G (Receipts No) for specific rows - but only
    into cells that are currently BLANK. Rows that already have something in column G
    are left untouched (could be a genuine pre-existing OR, or a sign the row was
    already posted under a different number - worth a manual look either way) and
    are returned instead of being silently overwritten.

    row_or_map: {sheet_row_number: "OR-2607123", ...}
    Returns: {sheet_row_number: {"existing": "...", "attempted": "OR-2607123"}, ...}
             for any row that was SKIPPED because column G wasn't blank.
    """
    if not row_or_map:
        return {}
    sh = client.open_by_key(spreadsheet_id)
    ws = sh.worksheet(tab_name)

    # Re-check current column G values right before writing, in case the sheet
    # changed since the dana list was loaded (e.g. a volunteer filled it in meanwhile)
    rows_sorted = sorted(row_or_map.keys())
    first_row, last_row = rows_sorted[0], rows_sorted[-1]
    current_g = ws.get(f"G{first_row}:G{last_row}")

    conflicts = {}
    updates = []
    for row, or_no in row_or_map.items():
        offset = row - first_row
        existing = ""
        if offset < len(current_g) and current_g[offset]:
            existing = str(current_g[offset][0]).strip()
        if existing:
            conflicts[row] = {"existing": existing, "attempted": or_no}
        else:
            updates.append({"range": f"G{row}", "values": [[or_no]]})

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    return conflicts


def list_tabs(client: gspread.Client, spreadsheet_id: str) -> list:
    """Return the names of all worksheet tabs in the spreadsheet, for a tab picker."""
    sh = client.open_by_key(spreadsheet_id)
    return [ws.title for ws in sh.worksheets()]


def append_rows(client: gspread.Client, spreadsheet_id: str, tab_name: str, rows: list) -> None:
    """
    Append new dana list rows to the end of an existing tab (e.g. entries confirmed
    from the AI slip-reading workflow). rows: list of 12-value lists matching
    DANA_LIST_HEADERS order. Does not touch any existing rows.
    """
    if not rows:
        return
    sh = client.open_by_key(spreadsheet_id)
    ws = sh.worksheet(tab_name)
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def find_unfilled_rows(client: gspread.Client, spreadsheet_id: str, tab_name: str) -> dict:
    """
    Build an index of rows in an existing bank-statement tab (columns A-F already
    filled by push_bank_statement, columns G-L still blank) so slip-extracted
    donations can be matched to the RIGHT existing row instead of creating a
    duplicate. Only rows where H (GL), I (description), J (donor), L (mobile) are
    ALL still blank are considered candidates - already-completed rows are excluded.

    Returns {(date_str, amount): [{"row": int, "desc1": str, "desc2": str,
    "beneficiary": str}, ...]} - a list per key since more than one transaction
    can share the same date+amount. The text fields let a caller disambiguate
    by fuzzy-matching against a known donor name when there are multiple candidates.
    """
    import re as _re
    sh = client.open_by_key(spreadsheet_id)
    ws = sh.worksheet(tab_name)
    values = ws.get_all_values()
    index = {}
    for i, row in enumerate(values[1:], start=2):   # row 1 = header
        if len(row) < 6:
            continue
        date_raw = row[0].strip()
        amount_raw = row[5].strip()
        if not date_raw or not amount_raw:
            continue
        # Columns H, I, J, L (indices 7, 8, 9, 11) must all be blank
        already_filled = any((row[idx].strip() if idx < len(row) else "") for idx in (7, 8, 9, 11))
        if already_filled:
            continue
        # Dates may be ISO ("2026-07-28", from push_bank_statement) or the sheet's
        # native "28 Jul 2026" text format - normalize both to YYYY-MM-DD
        try:
            date_str = pd.to_datetime(date_raw).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        # Amounts may be a bare number or "RM 288.00" - strip everything but digits/dot
        cleaned = _re.sub(r"[^\d.]", "", amount_raw)
        if not cleaned or cleaned == ".":
            continue
        try:
            amount = round(float(cleaned), 2)
        except ValueError:
            continue
        index.setdefault((date_str, amount), []).append({
            "row": i,
            "desc1": row[1].strip() if len(row) > 1 else "",
            "desc2": row[2].strip() if len(row) > 2 else "",
            "beneficiary": row[3].strip() if len(row) > 3 else "",
        })
    return index


def write_donor_details(client: gspread.Client, spreadsheet_id: str, tab_name: str,
                        row_details: dict) -> dict:
    """
    Write extracted donor details into columns H (GL), I (description), J (donor
    name), L (WhatsApp mobile) for specific EXISTING rows - matches an already
    pushed bank statement row instead of creating a new one. Column K (Whatsapp
    name) and G (Receipts No) are left untouched.

    Re-checks H/I/J/L right before writing and SKIPS any row that already has
    something in any of those columns - a row can look like the sole "unfilled"
    candidate at match-time but may belong to a different donor's own
    not-yet-processed transaction, or may have been filled by someone else since
    the match was computed. Never silently overwrite.

    row_details: {sheet_row_number: {"gl": "...", "description": "...",
                                     "donor": "...", "mobile": "..."}, ...}
    Returns: {sheet_row_number: {"existing_gl":.., "existing_description":..,
              "existing_donor":.., "existing_mobile":..}, ...} for any row
              that was SKIPPED because it already had data.
    """
    if not row_details:
        return {}
    sh = client.open_by_key(spreadsheet_id)
    ws = sh.worksheet(tab_name)

    rows_sorted = sorted(row_details.keys())
    first_row, last_row = rows_sorted[0], rows_sorted[-1]
    current = ws.get(f"H{first_row}:L{last_row}")   # columns H..L, 5 wide

    conflicts = {}
    updates = []
    for row, d in row_details.items():
        offset = row - first_row
        cur_row = current[offset] if offset < len(current) else []
        cur_row = cur_row + [""] * (5 - len(cur_row))   # pad to H,I,J,K,L
        existing_h, existing_i, existing_j, _existing_k, existing_l = cur_row
        if any(v.strip() for v in (existing_h, existing_i, existing_j, existing_l)):
            conflicts[row] = {
                "existing_gl": existing_h.strip(), "existing_description": existing_i.strip(),
                "existing_donor": existing_j.strip(), "existing_mobile": existing_l.strip(),
            }
            continue
        updates.append({"range": f"H{row}", "values": [[d.get("gl", "")]]})
        updates.append({"range": f"I{row}", "values": [[d.get("description", "")]]})
        updates.append({"range": f"J{row}", "values": [[d.get("donor", "")]]})
        updates.append({"range": f"L{row}", "values": [[d.get("mobile", "")]]})

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    return conflicts


def build_donor_phone_book(client: gspread.Client, spreadsheet_id: str,
                           exclude_tab: str = None) -> list:
    """
    Scan all monthly tabs (named as 6-digit YYYYMM, e.g. "202607") for donor name
    (column J) + WhatsApp mobile (column L) pairs, so a new donor's number can be
    suggested by fuzzy-matching their name against past months' records - useful
    when this month's WhatsApp text didn't include a phone number but the same
    donor gave one in a previous month.

    exclude_tab: skip this tab (typically the tab currently being worked on).
    Later (more recent) months overwrite earlier ones for the same donor text,
    so the most recently used number wins.

    Returns a list of (donor_name_text, mobile) tuples, most recent per unique
    donor text, ready for rapidfuzz.process.extractOne matching.
    """
    import re as _re
    sh = client.open_by_key(spreadsheet_id)
    month_tabs = sorted(
        (ws.title for ws in sh.worksheets() if _re.fullmatch(r"\d{6}", ws.title) and ws.title != exclude_tab)
    )
    phone_by_donor = {}
    for tab in month_tabs:
        ws = sh.worksheet(tab)
        values = ws.get_all_values()
        for row in values[1:]:
            if len(row) < 12:
                continue
            donor = row[9].strip()
            mobile = row[11].strip()
            if donor and mobile:
                phone_by_donor[donor] = mobile   # later tabs overwrite earlier ones
    return list(phone_by_donor.items())
