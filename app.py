# app.py -- Autocount Donation Receipt Web App
# Run with: python -m streamlit run app.py

import sys, io, re
import streamlit as st
import pandas as pd
import streamlit_authenticator as stauth

sys.path.insert(0, '.')
from parse_maybank import load_statement
from donation_mapping import map_to_gl, DONATION_MAP, get_department, GL_DEPARTMENT
from autocount_api import AutocountClient
from config_loader import MAYBANK_GL_CODE, DEFAULT_PAYMENT_METHOD

st.set_page_config(page_title="DE Penang Autocount Donation Receipts Apps", page_icon="ðŸ¦", layout="wide")

# ── Load users from GitHub (cloud) or local users.yaml
import base64, yaml, bcrypt, requests as _req

def _load_users_from_github() -> dict:
    try:
        token = st.secrets.get("GITHUB_TOKEN", "")
        if not token:
            raise ValueError("No GITHUB_TOKEN")
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
        r = _req.get("https://api.github.com/repos/crestcons-code/de-penang-receipt-bot/contents/users.yaml", headers=headers, timeout=10)
        r.raise_for_status()
        return yaml.safe_load(base64.b64decode(r.json()["content"]))
    except Exception:
        return None

def _load_users_local() -> dict:
    try:
        with open("users.yaml", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return {"usernames": {"crestcons": {"name": "Crestcons", "password": "$2b$12$q2OCd1uWqcXqgdmbTv7RweXAXB.ZZWbuf4ecghOr8Iw2Y8ZGY4HKy", "role": "admin"}}}

def _save_users_to_github(users_dict: dict) -> bool:
    try:
        token = st.secrets.get("GITHUB_TOKEN", "")
        if not token:
            return False
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
        url = "https://api.github.com/repos/crestcons-code/de-penang-receipt-bot/contents/users.yaml"
        sha = _req.get(url, headers=headers, timeout=10).json().get("sha", "")
        new_content = yaml.dump(users_dict, default_flow_style=False, allow_unicode=True)
        payload = {"message": "Update users", "content": base64.b64encode(new_content.encode()).decode(), "sha": sha}
        r = _req.put(url, headers=headers, json=payload, timeout=10)
        return r.status_code in (200, 201)
    except Exception:
        return False

_users_data = _load_users_from_github() or _load_users_local()

authenticator = stauth.Authenticate(
    _users_data,
    cookie_name="dep_receipt_app",
    cookie_key="dep_secret_key_2026",
    cookie_expiry_days=1,
)

authenticator.login()

if st.session_state.get("authentication_status") is False:
    st.error("Incorrect username or password.")
    st.stop()
elif st.session_state.get("authentication_status") is None:
    st.stop()

_current_user     = st.session_state.get("username", "")
_current_role     = _users_data.get("usernames", {}).get(_current_user, {}).get("role", "user")
_current_name     = st.session_state.get("name", _current_user)

with st.sidebar:
    st.markdown(f"**{_current_name}**")
    authenticator.logout("Logout")


# â"€â"€ GL code options for dropdown
GL_OPTIONS = {f"{code}  {desc}": code for code, desc, _ in DONATION_MAP}

# Reverse lookup: GL code â+' short description
GL_SHORT_DESC = {code: desc for code, desc, _ in DONATION_MAP}


def _parse_amount(val) -> float:
    """Parse 'RM 30.00' or '30.00' to float."""
    if pd.isna(val):
        return 0.0
    return float(re.sub(r"[^\d.]", "", str(val)))


def _gl_display(gl_code: str) -> str:
    """Return the GL_OPTIONS key string for a given GL code."""
    short = GL_SHORT_DESC.get(gl_code, gl_code)
    return f"{gl_code}  {short}"


def load_dana_list(file) -> pd.DataFrame:
    """
    Parse the dana list Excel file into the same internal format as the bank statement parser.
    Returns a DataFrame with columns: or_number, date, donor_name, gl_code, gl_display, description, department, amount
    """
    df = pd.read_excel(file, header=0, sheet_name=0)
    df.columns = [str(c).strip() for c in df.columns]

    # Identify columns by position (structure is fixed)
    cols = df.columns
    if len(cols) < 9:
        raise ValueError(f"Dana list has only {len(cols)} columns — expected at least 9. Please check the file format.")
    col_date     = cols[0]    # Transaction Date
    col_bene     = cols[3]    # Beneficiary / Biller Name
    col_amount   = cols[5]    # Transaction Amount: Cash-in (RM)
    col_or       = cols[6]    # Receipts No
    col_gl       = cols[7]    # accounting code
    col_desc     = cols[8]    # Dana description
    col_donor    = cols[9]  if len(cols) > 9  else None
    col_mobile   = cols[11] if len(cols) > 11 else None

    rows = []
    for _, r in df.iterrows():
        amount = _parse_amount(r[col_amount])
        if amount <= 0:
            continue

        # Date
        raw_date = r[col_date]
        if pd.isna(raw_date):
            continue
        txn_date = pd.to_datetime(raw_date).strftime("%Y-%m-%d")

        # Donor name: prefer receipt donor name (col J), fall back to beneficiary
        donor = ""
        if col_donor and pd.notna(r[col_donor]):
            donor = str(r[col_donor]).strip()
        if not donor or donor.lower() in ("nan", "none"):
            donor = str(r[col_bene]).strip().rstrip("*").strip()
        donor = donor.splitlines()[0].strip() if donor else donor

        # OR number (may be pre-filled or blank)
        or_no = str(r[col_or]).strip() if pd.notna(r[col_or]) else ""
        if or_no.lower() in ("nan", "none"):
            or_no = ""

        # GL code (may be pre-filled, multi-line, or blank - take first line only)
        gl_code = str(r[col_gl]).strip() if pd.notna(r[col_gl]) else ""
        if gl_code.lower() in ("nan", "none"):
            gl_code = ""
        gl_code = gl_code.splitlines()[0].strip() if gl_code else ""

        # Description — always from Dana description column (col I), first line only
        # Falls back to GL short desc only if col I is completely blank
        raw_desc = str(r[col_desc]).strip() if pd.notna(r[col_desc]) else ""
        description = raw_desc.splitlines()[0].strip() if raw_desc else ""

        # If GL not pre-filled, auto-map from transaction text
        if not gl_code:
            combined = f"{r.get(df.columns[1], '')} {r.get(df.columns[2], '')} {donor}"
            gl_code, _, auto_desc = map_to_gl(combined)
            if not description:
                description = auto_desc

        if not description:
            description = GL_SHORT_DESC.get(gl_code, "General Donation")

        mobile = ""
        if col_mobile and pd.notna(r[col_mobile]):
            mobile = str(r[col_mobile]).strip()
            if mobile.lower() in ("nan", "none"):
                mobile = ""

        rows.append({
            "or_number":   or_no,
            "date":        txn_date,
            "donor_name":  donor,
            "gl_code":     gl_code,
            "gl_display":  _gl_display(gl_code),
            "description": description,
            "department":  get_department(gl_code),
            "amount":      amount,
            "mobile":      mobile,
        })

    return pd.DataFrame(rows)


def render_review_and_post(rows: list, skipped_count: int = 0):
    """Render the shared Step 2 review table and Step 3 post section."""
    if skipped_count:
        st.warning(f"{skipped_count} transaction(s) already recorded in Autocount - excluded from this review.")

    if not rows:
        st.info("No new transactions to post.")
        return

    # Tick All / Untick All buttons
    if "post_all" not in st.session_state:
        st.session_state.post_all = True

    btn_col, spacer = st.columns([3, 13])
    with btn_col:
        b1, b2 = st.columns(2)
        if b1.button("✔ All", help="Tick All", use_container_width=True):
            st.session_state.post_all = True
            st.rerun()
        if b2.button("✘ All", help="Untick All", use_container_width=True):
            st.session_state.post_all = False
            st.rerun()

    df_rows = pd.DataFrame(rows)
    df_rows["Post"] = st.session_state.post_all

    edited = st.data_editor(
        df_rows,
        column_config={
            "Post":             st.column_config.CheckboxColumn("Post?", default=True),
            "OR Number":        st.column_config.TextColumn("OR Number", disabled=True),
            "Date":             st.column_config.TextColumn("Date", disabled=True),
            "Donor Name":       st.column_config.TextColumn("Donor Name", disabled=True),
            "GL Account":       st.column_config.SelectboxColumn("GL Account", options=list(GL_OPTIONS.keys())),
            "Description":      st.column_config.TextColumn("Description (in Autocount)"),
            "Department":       st.column_config.TextColumn("Department"),
            "Amount (RM)":      st.column_config.NumberColumn("Amount (RM)", format="RM %.2f", disabled=True),
            "WhatsApp Mobile":  st.column_config.TextColumn("WhatsApp Mobile", disabled=True),
        },
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
    )

    to_post   = edited[edited["Post"] == True]
    total_all = edited["Amount (RM)"].sum()
    total_sel = to_post["Amount (RM)"].sum()

    c1, c2, c3 = st.columns(3)
    c1.metric("Transactions", f"{len(edited)}")
    c2.metric("Selected for Posting", f"{len(to_post)}")
    c3.metric("Total Amount (RM)", f"{total_sel:,.2f}",
              delta=f"{total_all - total_sel:,.2f} excluded" if total_all != total_sel else None)
    st.divider()

    # â"€â"€ Step 3: Post to Autocount
    st.subheader("Step 3 - Post to Autocount")

    if len(to_post) == 0:
        st.warning("No rows selected. Tick at least one row to post.")
        return

    if st.button(f"Post {len(to_post)} Receipt(s) to Autocount", type="primary", use_container_width=True):
        client = AutocountClient()
        results = []
        progress = st.progress(0)
        status_box = st.empty()

        for i, (_, row) in enumerate(to_post.iterrows()):
            gl_code = GL_OPTIONS.get(row["GL Account"], row["GL Account"].split()[0])
            donor   = str(row["Donor Name"])
            amount  = float(row["Amount (RM)"])
            date    = str(row["Date"])
            desc    = str(row["Description"])
            or_no   = str(row.get("OR Number", "")).strip()
            dept    = str(row.get("Department", "")).strip()
            if dept.lower() in ("nan", "none", "-"):
                dept = ""

            status_box.info(f"Posting {i+1}/{len(to_post)}: {donor} - RM{amount:.2f}")

            try:
                result = client.create_donation_receipt(
                    receipt_date=date,
                    amount=amount,
                    bank_gl_code=MAYBANK_GL_CODE,
                    donation_gl_code=gl_code,
                    donor_name=donor,
                    payment_method=DEFAULT_PAYMENT_METHOD,
                    description=desc,
                    department=dept,
                    doc_no=or_no,
                )
                doc_no = result.get("docNo") or result.get("DocNo") or "posted"
                results.append({"Donor": donor, "Amount": amount, "GL": gl_code,
                                "Description": desc, "Doc No": doc_no, "Status": "success", "Notes": ""})
            except Exception as e:
                results.append({"Donor": donor, "Amount": amount, "GL": gl_code,
                                "Description": desc, "Doc No": None, "Status": "error", "Notes": str(e)})

            progress.progress((i + 1) / len(to_post))

        status_box.empty()
        st.divider()

        df_results = pd.DataFrame(results)
        ok     = (df_results["Status"] == "success").sum()
        errors = (df_results["Status"] == "error").sum()

        col1, col2, col3 = st.columns(3)
        col1.metric("Total Posted", len(results))
        col2.metric("Success", ok)
        col3.metric("Errors", errors)

        st.dataframe(
            df_results.style.apply(
                lambda row: ["background-color: #d4edda" if row["Status"] == "success"
                             else "background-color: #f8d7da"] * len(row), axis=1
            ),
            use_container_width=True, hide_index=True,
        )

        buf = io.BytesIO()
        df_results.to_excel(buf, index=False)
        st.download_button("Download Posting Report", buf.getvalue(),
                           file_name="posting_report.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
st.title("DE Penang Autocount Donation Receipts Apps")
st.caption("Persatuan Dhamma Malaysia (Malaysia Dhamma Society - Penang Branch)")
st.divider()

_tabs = ["Upload Bank Statement (CSV/PDF)", "Upload Dana List (Excel)", "Reconciliation"]
if _current_role == "admin":
    _tabs.append("Admin — Manage Users")
_tab_objs = st.tabs(_tabs)
tab_bank  = _tab_objs[0]
tab_dana  = _tab_objs[1]
tab_recon = _tab_objs[2]
tab_admin = _tab_objs[3] if _current_role == "admin" else None

# ==============================================
# TAB 1 - Bank Statement
# ==============================================
with tab_bank:
    st.subheader("Step 1 - Upload Maybank Statement")
    uploaded = st.file_uploader("Upload Maybank CSV or PDF statement", type=["csv", "pdf"], key="bank_upload")

    if uploaded:
        import tempfile, os
        suffix = ".csv" if uploaded.name.endswith(".csv") else ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(uploaded.read())
            tmp_path = tmp.name

        try:
            df_raw = load_statement(tmp_path)
            os.unlink(tmp_path)
        except Exception as e:
            st.error(f"Error reading file: {e}")
            st.stop()

        st.success(f"Found **{len(df_raw)} incoming payment(s)** totalling **RM {df_raw['credit'].sum():,.2f}**")
        st.divider()

        st.subheader("Step 2 - Review & Edit Before Posting")
        st.info("Check each row. Change GL Account or Description if needed. Uncheck rows you want to skip.")

        with st.spinner("Checking Autocount for existing records and last OR number..."):
            client_pre = AutocountClient()
            from_date = df_raw["date"].min().strftime("%Y-%m-%d")
            to_date   = df_raw["date"].max().strftime("%Y-%m-%d")
            posted    = client_pre.get_posted_receipts(from_date, to_date)
            posted_keys = {(p["date"], round(p["amount"], 2)) for p in posted}

            first_date = df_raw["date"].iloc[0].strftime("%Y-%m-%d")
            yy, mm = first_date[2:4], first_date[5:7]
            prefix = f"OR-{yy}{mm}"
            last_or = client_pre.get_last_or_number(prefix=prefix)
            next_seq = (int(last_or[len(prefix):]) + 1) if (last_or and last_or.startswith(prefix)) else 1

        rows = []
        skipped_count = 0
        seq = next_seq
        for _, txn in df_raw.iterrows():
            txn_date = txn["date"].strftime("%Y-%m-%d")
            t_yy, t_mm = txn_date[2:4], txn_date[5:7]
            amount = round(float(txn["credit"]), 2)

            if (txn_date, amount) in posted_keys:
                skipped_count += 1
                continue

            gl_code, gl_name, short_desc = map_to_gl(txn["gl_text"])
            rows.append({
                "Post":        True,
                "OR Number":   f"OR-{t_yy}{t_mm}{seq:03d}",
                "Date":        txn_date,
                "Donor Name":  txn["donor_name"],
                "GL Account":  f"{gl_code}  {short_desc}",
                "Description": short_desc,
                "Department":  get_department(gl_code),
                "Amount (RM)": txn["credit"],
            })
            seq += 1

        render_review_and_post(rows, skipped_count)

    else:
        st.info("Upload a Maybank bank statement CSV or PDF to get started.")

# ==============================================
# TAB 2 - Dana List Excel
# ==============================================
with tab_dana:
    st.subheader("Step 1 - Upload Dana List Excel")
    st.caption("Upload the monthly dana list Excel file (e.g. DEPG Dana list 2026 June.xlsx)")
    dana_file = st.file_uploader("Upload Dana List Excel", type=["xlsx"], key="dana_upload")

    if dana_file:
        try:
            df_dana = load_dana_list(dana_file)
        except Exception as e:
            st.error(f"Error reading dana list: {e}")
            st.stop()

        total_amt = df_dana["amount"].sum()
        st.success(f"Found **{len(df_dana)} donation(s)** totalling **RM {total_amt:,.2f}**")

        # Count pre-filled vs auto-assigned OR numbers needed
        pre_filled = df_dana["or_number"].ne("").sum()
        needs_or   = df_dana["or_number"].eq("").sum()
        if pre_filled:
            st.info(f"{pre_filled} rows have OR numbers pre-assigned. {needs_or} row(s) will be auto-numbered.")

        st.divider()
        st.subheader("Step 2 - Review & Edit Before Posting")
        st.info("Check each row. Change GL Account or Description if needed. Uncheck rows you want to skip.")

        # Auto-assign OR numbers only for rows that don't have one
        with st.spinner("Checking Autocount for existing records and last OR number..."):
            client_pre = AutocountClient()
            from_date = df_dana["date"].min()
            to_date   = df_dana["date"].max()
            posted    = client_pre.get_posted_receipts(from_date, to_date)
            posted_keys = {(p["date"], round(p["amount"], 2)) for p in posted}

            if needs_or > 0:
                sample_date = df_dana["date"].iloc[0]
                yy, mm = sample_date[2:4], sample_date[5:7]
                prefix = f"OR-{yy}{mm}"
                last_or = client_pre.get_last_or_number(prefix=prefix)
                next_seq = (int(last_or[len(prefix):]) + 1) if (last_or and last_or.startswith(prefix)) else 1
            else:
                next_seq = 1
                prefix = ""

        # Build set of OR numbers already in Autocount for fast lookup
        # Also keep base numbers (strip suffixes like -1, -2, -3) so OR-2606153 matches OR-2606153-1
        posted_or_numbers = set()
        for p in posted:
            doc = p["docNo"]
            posted_or_numbers.add(doc)
            # Add base number: OR-2606153-1 â+' also add OR-2606153
            import re as _re
            base = _re.sub(r"-\d+$", "", doc)
            posted_or_numbers.add(base)

        rows = []
        skipped_count = 0
        seq = next_seq
        for _, txn in df_dana.iterrows():
            txn_date = txn["date"]
            amount   = round(float(txn["amount"]), 2)

            # Skip if pre-filled OR number already exists in Autocount
            if txn["or_number"] and txn["or_number"] in posted_or_numbers:
                skipped_count += 1
                continue

            # Skip if no OR number and date+amount already posted
            if not txn["or_number"] and (txn_date, amount) in posted_keys:
                skipped_count += 1
                continue

            # Use pre-filled OR number or auto-assign
            if txn["or_number"]:
                or_no = txn["or_number"]
            else:
                t_yy, t_mm = txn_date[2:4], txn_date[5:7]
                or_no = f"OR-{t_yy}{t_mm}{seq:03d}"
                seq += 1

            rows.append({
                "Post":           True,
                "OR Number":      or_no,
                "Date":           txn_date,
                "Donor Name":     txn["donor_name"],
                "GL Account":     txn["gl_display"],
                "Description":    txn["description"],
                "Department":     txn["department"],
                "Amount (RM)":    amount,
                "WhatsApp Mobile": txn.get("mobile", ""),
            })

        render_review_and_post(rows, skipped_count)

    else:
        st.info("Upload the monthly dana list Excel file to get started.")

# ==============================================
# TAB 3 - Reconciliation
# ==============================================
with tab_recon:
    st.subheader("Reconciliation - Dana List / Bank Statement vs Autocount")
    st.caption("Verify every donation has been recorded in Autocount.")

    recon_source = st.radio("Reconcile using:", ["Dana List (Excel)", "Bank Statement (CSV)"], horizontal=True)
    st.divider()

    def _render_recon_results(result_rows: list, source_label: str):
        df_result = pd.DataFrame(result_rows)
        found   = df_result["Status"].str.startswith("Found").sum()
        missing = (df_result["Status"] == "MISSING").sum()
        total   = len(df_result)

        c1, c2, c3 = st.columns(3)
        c1.metric(f"Total in {source_label}", total)
        c2.metric("Found in Autocount", found)
        c3.metric("Missing in Autocount", missing,
                  delta=f"-{missing}" if missing else None, delta_color="inverse")

        if missing > 0:
            st.error(f"{missing} donation(s) from the {source_label} are NOT found in Autocount!")
        else:
            st.success(f"All donations in the {source_label} are recorded in Autocount.")

        st.divider()

        with st.expander("What do the status results mean?"):
            st.markdown("""
| Status | Meaning |
|--------|---------|
| **Found** | The OR number exists in Autocount. Receipt is recorded. |
| **Found (by date+amount)** | No OR number on this row, but a record with the same date and amount was found in Autocount. Likely already posted. |
| **MISSING** | This donation cannot be found in Autocount - it may have been skipped or not yet posted. Action required. |

**Colour guide:** ðŸŸ¢ Green = Found &nbsp;&nbsp; ðŸ"´ Red = Missing
            """)

        filter_opt = st.radio("Show:", ["All", "Missing only", "Found only"], horizontal=True, key="recon_filter")
        if filter_opt == "Missing only":
            df_show = df_result[df_result["Status"] == "MISSING"]
        elif filter_opt == "Found only":
            df_show = df_result[df_result["Status"].str.startswith("Found")]
        else:
            df_show = df_result

        def _row_color(row):
            if row["Status"] == "MISSING":
                return ["background-color: #f8d7da"] * len(row)
            return ["background-color: #d4edda"] * len(row)

        st.dataframe(df_show.style.apply(_row_color, axis=1), use_container_width=True, hide_index=True)

        buf = io.BytesIO()
        df_show.to_excel(buf, index=False)
        label = {"All": "All", "Missing only": "Missing Only", "Found only": "Found Only"}.get(filter_opt, "All")
        st.download_button(
            f"Download Reconciliation Report ({label})", buf.getvalue(),
            file_name=f"reconciliation_report_{label.lower().replace(' ','_')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def _build_ac_lookups(posted: list):
        import re as _re2
        ac_or_numbers = set()
        for p in posted:
            doc = p["docNo"]
            ac_or_numbers.add(doc)
            ac_or_numbers.add(_re2.sub(r"-\d+$", "", doc))
        ac_by_date_amount = {}
        for p in posted:
            key = (p["date"], round(p["amount"], 2))
            ac_by_date_amount.setdefault(key, []).append(p["docNo"])
        return ac_or_numbers, ac_by_date_amount

    # â"€â"€ Dana List reconciliation
    if recon_source == "Dana List (Excel)":
        recon_file = st.file_uploader("Upload Dana List Excel", type=["xlsx"], key="recon_dana_upload")

        if recon_file:
            try:
                df_recon = load_dana_list(recon_file)
            except Exception as e:
                st.error(f"Error reading dana list: {e}")
                st.stop()

            with st.spinner("Fetching OR records from Autocount..."):
                client_r = AutocountClient()
                posted_r = client_r.get_posted_receipts(df_recon["date"].min(), df_recon["date"].max())

            ac_or_numbers, ac_by_date_amount = _build_ac_lookups(posted_r)

            result_rows = []
            for _, txn in df_recon.iterrows():
                or_no  = txn["or_number"]
                amount = round(float(txn["amount"]), 2)
                if or_no and or_no in ac_or_numbers:
                    status, matched = "Found", or_no
                elif not or_no:
                    matches = ac_by_date_amount.get((txn["date"], amount), [])
                    status  = "Found (by date+amount)" if matches else "MISSING"
                    matched = ", ".join(matches)
                else:
                    status, matched = "MISSING", ""

                result_rows.append({
                    "Status":          status,
                    "OR Number":       or_no,
                    "Matched AC Doc":  matched,
                    "Date":            txn["date"],
                    "Donor Name":      txn["donor_name"],
                    "GL Code":         txn["gl_code"],
                    "Description":     txn["description"],
                    "Amount (RM)":     amount,
                    "WhatsApp Mobile": txn.get("mobile", ""),
                })

            _render_recon_results(result_rows, "Dana List")
        else:
            st.info("Upload the monthly dana list Excel file to run the reconciliation check.")

    # â"€â"€ Bank Statement reconciliation
    else:
        recon_csv = st.file_uploader("Upload Maybank Bank Statement CSV", type=["csv"], key="recon_bank_upload")

        if recon_csv:
            import tempfile, os
            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                tmp.write(recon_csv.read())
                tmp_path = tmp.name
            try:
                df_bank = load_statement(tmp_path)
                os.unlink(tmp_path)
            except Exception as e:
                st.error(f"Error reading bank statement: {e}")
                st.stop()

            with st.spinner("Fetching OR records from Autocount..."):
                client_r = AutocountClient()
                from_r   = df_bank["date"].min().strftime("%Y-%m-%d")
                to_r     = df_bank["date"].max().strftime("%Y-%m-%d")
                posted_r = client_r.get_posted_receipts(from_r, to_r)

            _, ac_by_date_amount = _build_ac_lookups(posted_r)

            result_rows = []
            for _, txn in df_bank.iterrows():
                txn_date = txn["date"].strftime("%Y-%m-%d")
                amount   = round(float(txn["credit"]), 2)
                matches  = ac_by_date_amount.get((txn_date, amount), [])
                status   = "Found (by date+amount)" if matches else "MISSING"
                matched  = ", ".join(matches)

                result_rows.append({
                    "Status":         status,
                    "Matched AC Doc": matched,
                    "Date":           txn_date,
                    "Donor Name":     txn["donor_name"],
                    "Amount (RM)":    amount,
                })

            _render_recon_results(result_rows, "Bank Statement")
        else:
            st.info("Upload the Maybank bank statement CSV to run the reconciliation check.")

# ==============================================
# TAB 4 - Admin: Manage Users (admin only)
# ==============================================
if tab_admin is not None:
    with tab_admin:
        st.subheader("User Management")
        st.caption("Add or remove users who can access this app.")

        users = _users_data.get("usernames", {})

        # ── Current users table
        st.markdown("**Current Users**")
        user_rows = [{"Username": u, "Name": v.get("name",""), "Role": v.get("role","user")} for u, v in users.items()]
        st.dataframe(user_rows, use_container_width=True, hide_index=True)
        st.divider()

        # ── Add new user
        st.markdown("**Add New User**")
        with st.form("add_user_form"):
            new_username = st.text_input("Username", placeholder="e.g. johndoe").strip().lower()
            new_name     = st.text_input("Full Name", placeholder="e.g. John Doe").strip()
            new_password = st.text_input("Password", type="password")
            new_role     = st.selectbox("Role", ["user", "admin"])
            submitted    = st.form_submit_button("Add User", type="primary")

        if submitted:
            if not new_username or not new_name or not new_password:
                st.error("Please fill in all fields.")
            elif new_username in users:
                st.error(f"Username '{new_username}' already exists.")
            else:
                import bcrypt as _bcrypt
                hashed = _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt()).decode()
                _users_data["usernames"][new_username] = {"name": new_name, "password": hashed, "role": new_role}
                if _save_users_to_github(_users_data):
                    st.success(f"User '{new_username}' added successfully! They can log in immediately.")
                    st.rerun()
                else:
                    st.error("Failed to save to GitHub. Check that GITHUB_TOKEN is set in Streamlit secrets.")

        st.divider()

        # ── Edit user
        st.markdown("**Edit User**")
        with st.form("edit_user_form"):
            edit_user     = st.selectbox("Select user to edit", list(users.keys()))
            edit_name     = st.text_input("New Display Name (leave blank to keep current)")
            edit_username = st.text_input("New Username (leave blank to keep current)").strip().lower()
            edit_password = st.text_input("New Password (leave blank to keep current)", type="password")
            edit_role     = st.selectbox("Role", ["user", "admin"])
            edit_btn      = st.form_submit_button("Save Changes", type="primary")

        if edit_btn:
            user_data = dict(users[edit_user])
            changed = False

            if edit_name:
                user_data["name"] = edit_name
                changed = True
            if edit_password:
                import bcrypt as _bcrypt
                user_data["password"] = _bcrypt.hashpw(edit_password.encode(), _bcrypt.gensalt()).decode()
                changed = True
            if edit_role != user_data.get("role", "user"):
                user_data["role"] = edit_role
                changed = True

            # Handle username change (rename key)
            target_username = edit_username if edit_username else edit_user
            if edit_username and edit_username != edit_user:
                if edit_username in users:
                    st.error(f"Username '{edit_username}' is already taken.")
                    changed = False
                else:
                    del _users_data["usernames"][edit_user]
                    _users_data["usernames"][target_username] = user_data
                    changed = True
            elif changed:
                _users_data["usernames"][edit_user] = user_data

            if changed:
                if _save_users_to_github(_users_data):
                    st.success(f"User '{target_username}' updated successfully.")
                    st.rerun()
                else:
                    st.error("Failed to save to GitHub.")
            else:
                st.info("No changes made.")

        st.divider()

        # ── Remove user
        st.markdown("**Remove User**")
        removable = [u for u in users if u != _current_user]
        if removable:
            with st.form("remove_user_form"):
                remove_user = st.selectbox("Select user to remove", removable)
                confirm     = st.checkbox(f"Yes, I want to remove '{remove_user}'")
                remove_btn  = st.form_submit_button("Remove User", type="secondary")
            if remove_btn:
                if not confirm:
                    st.warning("Please tick the confirmation checkbox.")
                else:
                    del _users_data["usernames"][remove_user]
                    if _save_users_to_github(_users_data):
                        st.success(f"User '{remove_user}' removed.")
                        st.rerun()
                    else:
                        st.error("Failed to save to GitHub.")
        else:
            st.info("No other users to remove.")
