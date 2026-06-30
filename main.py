# main.py -- Step 2: Post reviewed donations to Autocount
#
# Usage:
#   python main.py "C:\Users\SerVer2\Desktop\review_YYYYMMDD_HHMMSS.xlsx"

import sys
import os
import pandas as pd
from datetime import datetime
from autocount_api import AutocountClient
from config_loader import MAYBANK_GL_CODE, DEFAULT_PAYMENT_METHOD


def post(review_file: str):
    print("\n=== Step 2: Post Donations to Autocount ===")
    print(f"Review file : {review_file}\n")

    # Read reviewed Excel
    df = pd.read_excel(review_file, sheet_name="Donations", dtype=str)
    df.columns = df.columns.str.strip()

    # Filter only rows marked YES
    df["Post (YES/NO)"] = df["Post (YES/NO)"].str.strip().str.upper()
    to_post = df[df["Post (YES/NO)"] == "YES"].copy()
    skipped = len(df) - len(to_post)

    print(f"Total rows    : {len(df)}")
    print(f"Marked YES    : {len(to_post)}")
    print(f"Skipped (NO)  : {skipped}\n")

    if len(to_post) == 0:
        print("Nothing to post. Done.")
        return

    client  = AutocountClient()
    results = []

    for _, row in to_post.iterrows():
        or_number   = str(row.get("OR Number", "")).strip()
        if or_number.lower() in ("nan", "none", ""):
            or_number = ""
        date        = str(row["Date"]).strip()
        donor       = str(row["Donor Name"]).strip()
        gl_code     = str(row["GL Account Code"]).strip()
        gl_name     = str(row["GL Description"]).strip()
        description = str(row.get("Description (in Autocount)", gl_name)).strip()
        department  = str(row.get("Department", "")).strip()
        if department.lower() in ("nan", "none", "-"):
            department = ""
        amount      = float(str(row["Amount (RM)"]).replace(",", "").strip())

        result_row = {
            "Date":       date,
            "Donor Name": donor,
            "GL Code":    gl_code,
            "GL Name":    gl_name,
            "Amount":     amount,
            "Doc No":     None,
            "Status":     None,
            "Notes":      "",
        }

        try:
            result = client.create_donation_receipt(
                receipt_date=date,
                amount=amount,
                bank_gl_code=MAYBANK_GL_CODE,
                donation_gl_code=gl_code,
                donor_name=donor,
                payment_method=DEFAULT_PAYMENT_METHOD,
                description=description,
                department=department,
                doc_no=or_number,
            )
            result_row["Doc No"] = result.get("docNo") or result.get("DocNo") or "posted"
            result_row["Status"] = "success"
            print(f"  [OK]  {donor[:30]:30}  RM{amount:>9.2f}  {gl_code}  {gl_name}")
        except Exception as e:
            result_row["Status"] = "error"
            result_row["Notes"]  = str(e)
            print(f"  [ERR] {donor[:30]:30}  RM{amount:>9.2f}  {e}")

        results.append(result_row)

    # Save posting report to Desktop
    print("\nSaving posting report...")
    desktop  = os.path.join(os.path.expanduser("~"), "Desktop")
    out_path = os.path.join(desktop, f"posted_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
    pd.DataFrame(results).to_excel(out_path, index=False)

    ok     = sum(1 for r in results if r["Status"] == "success")
    errors = len(results) - ok

    print(f"\n--- Summary ---")
    print(f"  Posted    : {ok}")
    print(f"  Errors    : {errors}")
    print(f"  Report    : {out_path}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python main.py <review_file.xlsx>")
        sys.exit(1)
    post(sys.argv[1])
