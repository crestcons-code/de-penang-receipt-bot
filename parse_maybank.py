# parse_maybank.py - reads Maybank CSV or PDF bank statement

import pandas as pd
import pdfplumber
from pathlib import Path


def parse_csv(filepath: str) -> pd.DataFrame:
    """
    Parse Maybank CSV statement (Maybank2u download format).
    Columns: Transaction Date | Description 1 | Description 2 |
             Beneficiary/Biller Name | Account | Cash-in | Cash-out
    Returns only incoming (cash-in) transactions.
    """
    # Skip the first 3 header info rows
    df = pd.read_csv(filepath, skiprows=3, encoding="utf-8-sig", dtype=str)
    df.columns = df.columns.str.strip()

    # Rename to standard names
    df = df.rename(columns={
        "Transaction Date": "date",
        "Transaction Description 1": "desc1",
        "Transaction Description 2": "desc2",
        "Beneficiary/ Biller Name": "beneficiary",
        "Transaction Amount: Cash-in (RM)": "credit",
        "Transaction Amount: Cash-out (RM)": "debit",
    })

    # Clean whitespace from all string columns
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()

    # Parse amount - remove "RM " prefix and commas
    df["credit"] = (
        df["credit"]
        .str.replace("RM", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.strip()
    )
    df["credit"] = pd.to_numeric(df["credit"], errors="coerce")

    # Keep only cash-in (incoming) rows
    df = df[df["credit"] > 0].copy()

    # Parse date
    df["date"] = pd.to_datetime(df["date"], format="%d %b %Y", errors="coerce")
    df = df.dropna(subset=["date"])

    # Clean beneficiary name (strip trailing asterisk)
    df["beneficiary"] = df["beneficiary"].str.replace(r"\s*\*+\s*$", "", regex=True).str.strip()

    # donor_name: what appears in Autocount as "Deal With" - use beneficiary name
    df["donor_name"] = df["beneficiary"]

    # gl_text: combined text used for GL keyword matching
    # Priority: desc1 + desc2 give the donation purpose; beneficiary name is fallback
    def build_gl_text(row):
        parts = []
        for col in ["desc1", "desc2", "beneficiary"]:
            val = str(row[col]).strip()
            if val and val.lower() not in ("nan", "none", ""):
                parts.append(val)
        return " ".join(parts)

    df["gl_text"] = df.apply(build_gl_text, axis=1)

    # description shown in Autocount - same as donor_name (GL mapping uses gl_text separately)
    df["description"] = df["donor_name"]

    df["source_file"] = Path(filepath).name
    return df[["date", "donor_name", "description", "gl_text", "credit", "source_file"]].reset_index(drop=True)


def parse_pdf(filepath: str) -> pd.DataFrame:
    """Parse Maybank PDF statement - extracts the transaction table."""
    rows = []
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            for row in table:
                if row and len(row) >= 4:
                    rows.append(row)

    if not rows:
        raise ValueError(f"No table data found in PDF: {filepath}")

    df = pd.DataFrame(rows[1:], columns=rows[0])
    df.columns = df.columns.str.strip().str.lower()

    col_map = {}
    for col in df.columns:
        if "date" in col:
            col_map[col] = "date"
        elif "desc" in col or "particular" in col or "narration" in col:
            col_map[col] = "description"
        elif "credit" in col or "deposits" in col or "cash-in" in col:
            col_map[col] = "credit"
    df = df.rename(columns=col_map)

    df["credit"] = pd.to_numeric(
        df["credit"].astype(str).str.replace("RM", "").str.replace(",", "").str.strip(),
        errors="coerce"
    )
    df = df[df["credit"] > 0].copy()
    df["date"] = pd.to_datetime(df["date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["date"])
    df["description"] = df["description"].astype(str).str.strip()
    df["source_file"] = Path(filepath).name

    return df[["date", "description", "credit", "source_file"]].reset_index(drop=True)


def load_statement(filepath: str) -> pd.DataFrame:
    """Auto-detect CSV or PDF and return standardised transaction DataFrame."""
    path = Path(filepath)
    if path.suffix.lower() == ".csv":
        return parse_csv(filepath)
    elif path.suffix.lower() == ".pdf":
        return parse_pdf(filepath)
    else:
        raise ValueError(f"Unsupported file type: {path.suffix}")
