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
    """
    Parse Maybank PDF statement (text layout, no table grid).
    Transaction line format:
        DD/MM [DD/MM] DESCRIPTION  9,999.99+  99,999.99[DR]
    where + = cash-in, - = cash-out. Continuation lines that follow hold the
    sender/beneficiary name (usually ending with '*') and the transfer purpose.
    Returns the same standardised columns as parse_csv (cash-in rows only).
    """
    import re as _re

    txn_re = _re.compile(
        r"^(\d{2}/\d{2})(?:\s+\d{2}/\d{2})?\s+(.*?)\s+([\d,]*\.\d{2})([+-])\s+[\d,]*\.\d{2}(?:DR)?$"
    )
    stop_re = _re.compile(r"BAKI LEGAR|LEDGER BALANCE|^Perhatian|ENDING BALANCE|TOTAL DEBIT|TOTAL CREDIT")
    year_re = _re.compile(r":\s*(\d{2})/(\d{2})/(\d{2})\b")

    stmt_year = None
    txns = []       # each: {"date_dm","desc_line","amount","sign","extra":[...]}
    current = None

    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines = text.splitlines()

            # Statement year from "TARIKH PENYATA ... : 30/06/26" on page 1
            if stmt_year is None:
                for ln in lines:
                    m = year_re.search(ln)
                    if m:
                        stmt_year = 2000 + int(m.group(3))
                        break

            in_txn_area = False
            for ln in lines:
                ln = ln.strip()
                if not in_txn_area:
                    # Transactions start after the column header line
                    if "TRANSACTION DESCRIPTION" in ln or "ENTRY DATE" in ln:
                        in_txn_area = True
                    continue
                if stop_re.search(ln):
                    current = None
                    break
                m = txn_re.match(ln)
                if m:
                    current = {
                        "date_dm":   m.group(1),
                        "desc_line": m.group(2).strip(),
                        "amount":    float(m.group(3).replace(",", "")),
                        "sign":      m.group(4),
                        "extra":     [],
                    }
                    txns.append(current)
                elif current is not None and ln:
                    current["extra"].append(ln)

    if not txns:
        raise ValueError(f"No transactions found in PDF: {filepath}")

    if stmt_year is None:
        stmt_year = pd.Timestamp.today().year

    rows = []
    for t in txns:
        if t["sign"] != "+":
            continue  # cash-in only

        # First extra line ending with '*' is the sender/beneficiary name
        beneficiary = ""
        purpose_parts = []
        for ln in t["extra"]:
            if not beneficiary and ln.endswith("*"):
                beneficiary = ln.rstrip("*").strip()
            else:
                purpose_parts.append(ln)
        if not beneficiary and t["extra"]:
            beneficiary = t["extra"][0]
            purpose_parts = t["extra"][1:]

        dd, mm = t["date_dm"].split("/")
        date = pd.Timestamp(year=stmt_year, month=int(mm), day=int(dd))

        gl_text = " ".join([t["desc_line"]] + purpose_parts + ([beneficiary] if beneficiary else []))

        rows.append({
            "date":        date,
            "donor_name":  beneficiary,
            "description": beneficiary,
            "gl_text":     gl_text,
            "credit":      t["amount"],
            "source_file": Path(filepath).name,
        })

    if not rows:
        raise ValueError(f"No cash-in transactions found in PDF: {filepath}")

    return pd.DataFrame(rows)[["date", "donor_name", "description", "gl_text", "credit", "source_file"]].reset_index(drop=True)


def load_statement(filepath: str) -> pd.DataFrame:
    """Auto-detect CSV or PDF and return standardised transaction DataFrame."""
    path = Path(filepath)
    if path.suffix.lower() == ".csv":
        return parse_csv(filepath)
    elif path.suffix.lower() == ".pdf":
        return parse_pdf(filepath)
    else:
        raise ValueError(f"Unsupported file type: {path.suffix}")
