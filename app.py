# app.py -- Autocount Donation Receipt Web App
# Run with: python -m streamlit run app.py

import sys, io, re, time, json
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
    cookie_expiry_days=30,
)

# Silent cookie re-login (no form rendered) - keeps returning users signed in
authenticator.login(location="unrendered")

if not st.session_state.get("authentication_status"):
    # Custom login form: pick your name from a dropdown instead of typing the username
    st.markdown('<h1 style="color:#2563eb;">DE Penang Autocount Donation Receipts Apps</h1>', unsafe_allow_html=True)
    _all_users = _users_data.get("usernames", {})
    _display_to_username = {f"{v.get('name', u)} ({u})": u for u, v in _all_users.items()}

    with st.form("dropdown_login"):
        _sel_display = st.selectbox("Select user", list(_display_to_username.keys()))
        _pwd = st.text_input("Password", type="password")
        _login_btn = st.form_submit_button("Login", type="primary", use_container_width=True)

    if _login_btn:
        _sel_username = _display_to_username[_sel_display]
        try:
            _ok = authenticator.authentication_controller.login(username=_sel_username, password=_pwd)
        except Exception:
            _ok = False
        if _ok is False or not st.session_state.get("authentication_status"):
            st.error("Incorrect password.")
            st.stop()
        authenticator.cookie_controller.set_cookie()
        st.rerun()
    else:
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
    """Return the GL_OPTIONS key string for a given GL code, or bare code if unknown."""
    short = GL_SHORT_DESC.get(gl_code)
    return f"{gl_code}  {short}" if short else gl_code


def load_dana_list(file, skip_blank_gl=True) -> pd.DataFrame:
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
    col_desc1    = cols[1]    # Transaction Description 1
    col_desc2    = cols[2]    # Transaction Description 2
    col_bene     = cols[3]    # Beneficiary / Biller Name
    col_amount   = cols[5]    # Transaction Amount: Cash-in (RM)
    col_or       = cols[6]    # Receipts No
    col_gl       = cols[7]    # accounting code
    col_desc     = cols[8]    # Dana description
    col_donor    = cols[9]  if len(cols) > 9  else None
    col_mobile   = cols[11] if len(cols) > 11 else None

    # Forest monastery donor-group code, e.g. "RC G7" (found in the bank transaction
    # descriptions or dana description). When present, the Autocount description
    # becomes "RC G7 - <Donor Name>" so the donor group is identifiable.
    _rc_group_re = re.compile(r"RC\s*-?\s*G\s*-?\s*(\d+)", re.IGNORECASE)

    rows = []
    blank_gl_count = [0]  # mutable counter
    for _, r in df.iterrows():
        amount = _parse_amount(r[col_amount])
        if amount <= 0:
            continue

        # Date
        raw_date = r[col_date]
        if pd.isna(raw_date):
            continue
        txn_date = pd.to_datetime(raw_date).strftime("%Y-%m-%d")

        # Donor name: col J (Donor name on Receipt) first, fall back to col D (Beneficiary)
        # Multi-donor cells have one donor per line - join them all onto the one receipt
        raw_donor_cell = str(r[col_donor]).strip() if (col_donor and pd.notna(r[col_donor])) else ""
        donor = raw_donor_cell
        if not donor or donor.lower() in ("nan", "none", "-", "n/a"):
            donor = str(r[col_bene]).strip().rstrip("*").strip()
        donor = ", ".join(l.strip() for l in donor.splitlines() if l.strip()) if donor else ""

        # Multi-donor rows with per-donor amounts (e.g. "1) Lim Bee Chin RM30") become
        # separate detail lines on the one receipt - only when the amounts add up
        detail_lines = []
        if raw_donor_cell and "\n" in raw_donor_cell:
            _line_re = re.compile(r"^\s*(?:\d+[\).\:]\s*)?(.+?)\s*[-–]?\s*RM\s*([\d,]+(?:\.\d{1,2})?)\s*$", re.IGNORECASE)
            _parsed = []
            for _ln in raw_donor_cell.splitlines():
                _ln = _ln.strip()
                if not _ln:
                    continue
                _m = _line_re.match(_ln)
                if not _m:
                    _parsed = []
                    break
                _parsed.append({"description": _m.group(1).strip(),
                                "amount": float(_m.group(2).replace(",", ""))})
            if len(_parsed) > 1 and abs(sum(p["amount"] for p in _parsed) - amount) < 0.01:
                detail_lines = _parsed

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

        # Skip rows where column H (accounting code) is blank (only when posting receipts)
        if not gl_code:
            blank_gl_count[0] += 1
            if skip_blank_gl:
                continue

        if not description:
            description = GL_SHORT_DESC.get(gl_code, "General Donation")

        # Forest monastery: prefix the donor-group code (RC G7 etc.) to the donor name
        _rc_src = " ".join(
            str(r[c]) for c in (col_desc1, col_desc2, col_desc)
            if pd.notna(r[c])
        )
        _rc_m = _rc_group_re.search(_rc_src)
        if _rc_m:
            description = f"RC G{_rc_m.group(1)} - {donor}"

        mobile = ""
        if col_mobile and pd.notna(r[col_mobile]):
            mobile = str(r[col_mobile]).strip()
            if mobile.lower() in ("nan", "none"):
                mobile = ""
            # Multi-donor rows may list one contact number per line - keep them all
            mobile = ", ".join(l.strip() for l in mobile.splitlines() if l.strip()) if mobile else ""

        rows.append({
            "detail_json": json.dumps(detail_lines) if detail_lines else "",
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

    return pd.DataFrame(rows), blank_gl_count[0]


def _post_rows(post_items: list, existing_or_numbers: set = None) -> list:
    """Post a list of row-dicts to Autocount. Each item needs: OR Number, Date, Donor Name,
    GL Account (code), Description, Department, Amount (RM), WhatsApp Mobile.
    existing_or_numbers: pre-fetched set of OR numbers already in Autocount (avoids an
    unreliable live per-row API check - Autocount's listing endpoint ignores docNo filters).
    Returns a list of result dicts, preserving the intended OR Number even on failure."""
    client = AutocountClient()
    results = []
    progress = st.progress(0)
    status_box = st.empty()
    existing_or_numbers = existing_or_numbers or set()

    for i, item in enumerate(post_items):
        gl_code = item["GL Account"]
        donor   = str(item["Donor Name"])
        amount  = float(item["Amount (RM)"])
        date    = str(item["Date"])
        desc    = str(item["Description"])
        or_no   = str(item.get("OR Number", "")).strip()
        dept    = str(item.get("Department", "")).strip()
        if dept.lower() in ("nan", "none", "-"):
            dept = ""
        whatsapp = str(item.get("WhatsApp Mobile", "")).strip()

        # Multi-donor receipts: one detail line per donor with their own amount
        detail_lines = []
        _dj = str(item.get("_details", "") or "").strip()
        if _dj and _dj.lower() not in ("nan", "none"):
            try:
                detail_lines = json.loads(_dj)
            except Exception:
                detail_lines = []

        status_box.info(f"Posting {i+1}/{len(post_items)}: {donor} - RM{amount:.2f} ({or_no})")

        # If a specific OR number was intended and it already exists in Autocount,
        # it means an earlier attempt actually succeeded despite an error response.
        # Don't re-post (would create a duplicate) - just record it as already done.
        if or_no and or_no in existing_or_numbers:
            results.append({"Donor": donor, "Amount": amount, "GL": gl_code,
                            "Description": desc, "Doc No": or_no, "OR Number": or_no,
                            "Date": date, "Department": dept, "WhatsApp": whatsapp,
                            "Status": "success", "Notes": "Already existed in Autocount (earlier attempt had succeeded)"})
            time.sleep(0.3)
            progress.progress((i + 1) / len(post_items))
            continue

        last_err = None
        for attempt in range(3):
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
                    strict_doc_no=True,
                    detail_lines=detail_lines,
                )
                doc_no = result.get("docNo") or result.get("DocNo") or or_no or "posted"
                results.append({"Donor": donor, "Amount": amount, "GL": gl_code,
                                "Description": desc, "Doc No": doc_no, "OR Number": or_no,
                                "Date": date, "Department": dept, "WhatsApp": whatsapp,
                                "Status": "success", "Notes": ""})
                last_err = None
                break
            except Exception as e:
                last_err = e
                if "429" in str(e):
                    wait = 5 * (attempt + 1)
                    status_box.warning(f"Rate limited — waiting {wait}s before retry ({attempt+1}/3)...")
                    time.sleep(wait)
                else:
                    break

        if last_err is not None:
            results.append({"Donor": donor, "Amount": amount, "GL": gl_code,
                            "Description": desc, "Doc No": None, "OR Number": or_no,
                            "Date": date, "Department": dept, "WhatsApp": whatsapp,
                            "Status": "error", "Notes": str(last_err)})

        time.sleep(0.3)
        progress.progress((i + 1) / len(post_items))

    status_box.empty()
    return results


def render_review_and_post(rows: list, skipped_count: int = 0, existing_or_numbers: set = None):
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

    # Build options list that includes any GL codes from the data not already in GL_OPTIONS
    all_gl_options = list(GL_OPTIONS.keys())
    for gl_val in df_rows["GL Account"].dropna().unique():
        if gl_val not in GL_OPTIONS:
            all_gl_options.append(gl_val)

    edited = st.data_editor(
        df_rows,
        column_config={
            "Post":             st.column_config.CheckboxColumn("Post?", default=True),
            "OR Number":        st.column_config.TextColumn("OR Number", help="Edit to reuse a specific missing OR number"),
            "Date":             st.column_config.TextColumn("Date", disabled=True),
            "Donor Name":       st.column_config.TextColumn("Donor Name"),
            "GL Account":       st.column_config.SelectboxColumn("GL Account", options=all_gl_options),
            "Description":      st.column_config.TextColumn("Description (in Autocount)"),
            "Department":       st.column_config.TextColumn("Department"),
            "Amount (RM)":      st.column_config.NumberColumn("Amount (RM)", format="RM %.2f", disabled=True),
            "WhatsApp Mobile":  st.column_config.TextColumn("WhatsApp Mobile", help="Edit to add or correct the donor's WhatsApp number"),
            "_details":         None,   # hidden: per-donor detail lines (JSON) for multi-donor receipts
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
        post_items = []
        for _, row in to_post.iterrows():
            post_items.append({
                "OR Number":       str(row.get("OR Number", "")).strip(),
                "Date":            str(row["Date"]),
                "Donor Name":      str(row["Donor Name"]),
                "GL Account":      GL_OPTIONS.get(row["GL Account"]) or row["GL Account"].split()[0],
                "Description":     str(row["Description"]),
                "Department":      str(row.get("Department", "")).strip(),
                "Amount (RM)":     float(row["Amount (RM)"]),
                "WhatsApp Mobile": str(row.get("WhatsApp Mobile", "")).strip(),
                "_details":        str(row.get("_details", "") or ""),
            })
        st.session_state["dana_post_results"] = _post_rows(post_items, existing_or_numbers)

    if "dana_post_results" in st.session_state:
        results = st.session_state["dana_post_results"]
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

        if errors > 0:
            st.warning(f"{errors} row(s) failed to post. Their intended OR number(s) were NOT created in Autocount, "
                       "so retrying below will reuse the same OR number and avoid a gap in the sequence.")
            if st.button(f"🔁 Retry {errors} Failed Row(s) with Same OR Number", type="primary"):
                failed_items = [
                    {"OR Number": r["OR Number"], "Date": r["Date"], "Donor Name": r["Donor"],
                     "GL Account": r["GL"], "Description": r["Description"], "Department": r["Department"],
                     "Amount (RM)": r["Amount"], "WhatsApp Mobile": r["WhatsApp"]}
                    for r in results if r["Status"] == "error"
                ]
                # Re-fetch current OR numbers before retry, in case Autocount state changed
                with st.spinner("Re-checking Autocount before retry..."):
                    retry_client = AutocountClient()
                    retry_dates = [f["Date"] for f in failed_items]
                    fresh_posted = retry_client.get_posted_receipts(min(retry_dates), max(retry_dates))
                    fresh_existing = {p["docNo"] for p in fresh_posted}
                retry_results = _post_rows(failed_items, fresh_existing)
                # Replace error entries with their retry outcome, keep successes as-is
                new_results = [r for r in results if r["Status"] == "success"] + retry_results
                st.session_state["dana_post_results"] = new_results
                st.rerun()

        buf = io.BytesIO()
        df_results.to_excel(buf, index=False)
        st.download_button("Download Posting Report", buf.getvalue(),
                           file_name="posting_report.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        # Donor list with updated OR numbers and WhatsApp
        df_donor = df_results[df_results["Status"] == "success"][
            ["Date", "Doc No", "Donor", "Amount", "GL", "Description", "WhatsApp"]
        ].rename(columns={
            "Date":        "Transaction Date",
            "Doc No":      "OR Number",
            "Donor":       "Donor Name",
            "Amount":      "Amount (RM)",
            "GL":          "GL Code",
            "Description": "Description",
            "WhatsApp":    "WhatsApp Mobile",
        })
        if not df_donor.empty:
            buf2 = io.BytesIO()
            df_donor.to_excel(buf2, index=False)
            st.download_button("Download Donor List (OR + WhatsApp)", buf2.getvalue(),
                               file_name="donor_list_posted.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        # â"€â"€ Step 4: Print batch OR in Autocount
        success_ors = df_results[df_results["Status"] == "success"]["Doc No"].dropna().tolist()
        if success_ors:
            st.divider()
            st.subheader("Step 4 - Print Batch OR in Autocount")
            st.markdown("""
Follow these steps in **Autocount Cloud** to print and save all successfully posted receipts as one PDF:
1. Open **Cash Book Entry**
2. Click **Print Listing**
3. Select the OR numbers listed below (search/filter by OR number)
4. Tick all matching rows, click **Print**
5. Choose **Save as PDF** and store it in your receipts folder
            """)
            or_list_text = "\n".join(success_ors)
            st.text_area(f"OR Numbers to Print ({len(success_ors)} receipt(s)) - copy this list",
                        value=or_list_text, height=150)
            st.download_button("Download OR Number List (.txt)", or_list_text,
                               file_name="or_numbers_to_print.txt", mime="text/plain")


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
st.markdown('<h1 style="color:#2563eb;">DE Penang Autocount Donation Receipts Apps</h1>', unsafe_allow_html=True)
st.caption("Persatuan Dhamma Malaysia (Malaysia Dhamma Society - Penang Branch)")
st.divider()

_tabs = ["Upload Bank Statement (CSV/PDF)", "Upload Dana List (Excel)", "Reconciliation", "Print Batch OR"]
if _current_role == "admin":
    _tabs.append("Admin — Manage Users")
_tab_objs = st.tabs(_tabs)
tab_bank  = _tab_objs[0]
tab_dana  = _tab_objs[1]
tab_recon = _tab_objs[2]
tab_print = _tab_objs[3]
tab_admin = _tab_objs[4] if _current_role == "admin" else None

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
        st.info("Check each row. Change GL Account, Description, or OR Number if needed (e.g. to reuse a missing OR number from a failed post). Uncheck rows you want to skip.")

        with st.spinner("Checking Autocount for existing records and last OR number..."):
            client_pre = AutocountClient()
            from_date = df_raw["date"].min().strftime("%Y-%m-%d")
            to_date   = df_raw["date"].max().strftime("%Y-%m-%d")
            posted    = client_pre.get_posted_receipts(from_date, to_date)
            posted_keys = {(p["date"], round(p["amount"], 2)) for p in posted}
            posted_or_numbers = {p["docNo"] for p in posted}

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

        render_review_and_post(rows, skipped_count, existing_or_numbers=posted_or_numbers)

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
            df_dana, blank_gl = load_dana_list(dana_file)
        except Exception as e:
            st.error(f"Error reading dana list: {e}")
            st.stop()

        total_amt = df_dana["amount"].sum()
        st.success(f"Found **{len(df_dana)} donation(s)** totalling **RM {total_amt:,.2f}**")
        if blank_gl > 0:
            st.warning(f"{blank_gl} row(s) skipped — column H (accounting code) is blank. Please fill in the GL code in the dana list and re-upload.")

        # Count pre-filled vs auto-assigned OR numbers needed
        pre_filled = df_dana["or_number"].ne("").sum()
        needs_or   = df_dana["or_number"].eq("").sum()
        if pre_filled:
            st.info(f"{pre_filled} rows have OR numbers pre-assigned. {needs_or} row(s) will be auto-numbered.")

        st.divider()
        st.subheader("Step 2 - Review & Edit Before Posting")
        st.info("Check each row. Change GL Account, Description, or OR Number if needed (e.g. to reuse a missing OR number from a failed post). Uncheck rows you want to skip.")

        # Auto-assign OR numbers only for rows that don't have one
        with st.spinner("Checking Autocount for existing records and last OR number..."):
            client_pre = AutocountClient()
            from_date = df_dana["date"].min()
            to_date   = df_dana["date"].max()
            posted    = client_pre.get_posted_receipts(from_date, to_date)
            posted_keys = {(p["date"], round(p["amount"], 2), p["dealWith"]) for p in posted}

            gap_queue = []
            if needs_or > 0:
                sample_date = df_dana["date"].iloc[0]
                yy, mm = sample_date[2:4], sample_date[5:7]
                prefix = f"OR-{yy}{mm}"
                last_or = client_pre.get_last_or_number(prefix=prefix)
                max_used = int(last_or[len(prefix):]) if (last_or and last_or.startswith(prefix)) else 0

                # Find any gap numbers (missing OR-YYMMNNN) within this month's range so they
                # get backfilled automatically instead of creating new gaps further down.
                used_nums = set()
                for p in posted:
                    doc = p["docNo"]
                    if doc.startswith(prefix) and doc[len(prefix):].isdigit():
                        used_nums.add(int(doc[len(prefix):]))
                gap_queue = [n for n in range(1, max_used) if n not in used_nums]

                next_seq = max_used + 1
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
        skipped_rows = []   # display info
        skipped_txns = []   # full txn data for potential re-include
        skipped_count = 0
        seq = next_seq
        for _, txn in df_dana.iterrows():
            txn_date = txn["date"]
            amount   = round(float(txn["amount"]), 2)

            # Skip if pre-filled OR number already exists in Autocount
            if txn["or_number"] and txn["or_number"] in posted_or_numbers:
                skipped_count += 1
                skipped_rows.append({"Re-post?": False, "OR Number": txn["or_number"], "Date": txn_date,
                                     "Donor": txn["donor_name"], "Amount (RM)": amount,
                                     "Reason": "OR number already in Autocount"})
                skipped_txns.append(txn)
                continue

            # Skip if no OR number and date+amount+donor already posted
            donor_key = str(txn["donor_name"]).strip().upper()
            if not txn["or_number"] and (txn_date, amount, donor_key) in posted_keys:
                skipped_count += 1
                skipped_rows.append({"Re-post?": False, "OR Number": "(none)", "Date": txn_date,
                                     "Donor": txn["donor_name"], "Amount (RM)": amount,
                                     "Reason": "Same date, amount & donor already in Autocount"})
                skipped_txns.append(txn)
                continue

            # Use pre-filled OR number, or fill a gap number first, then continue the sequence
            if txn["or_number"]:
                or_no = txn["or_number"]
            else:
                t_yy, t_mm = txn_date[2:4], txn_date[5:7]
                if gap_queue:
                    or_no = f"OR-{t_yy}{t_mm}{gap_queue.pop(0):03d}"
                else:
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
                "_details":       txn.get("detail_json", ""),
            })

        if skipped_rows:
            with st.expander(f"⚠️ {skipped_count} row(s) skipped — already found in Autocount (click to view / re-post)"):
                st.caption("Tick **Re-post?** on any row you believe was NOT actually posted, then click the button below to add it to Step 2.")
                edited_skipped = st.data_editor(
                    pd.DataFrame(skipped_rows),
                    column_config={"Re-post?": st.column_config.CheckboxColumn("Re-post?", default=False)},
                    use_container_width=True, hide_index=True,
                    key="skipped_editor",
                )
                if st.button("↩️ Add selected rows back to Step 2", key="reinclude_btn"):
                    reinclude_idx = edited_skipped.index[edited_skipped["Re-post?"] == True].tolist()
                    for idx in reinclude_idx:
                        txn = skipped_txns[idx]
                        txn_date = txn["date"]
                        amount   = round(float(txn["amount"]), 2)
                        or_no = txn["or_number"] if txn["or_number"] else f"OR-{txn_date[2:4]}{txn_date[5:7]}{seq:03d}"
                        if not txn["or_number"]:
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
                    if reinclude_idx:
                        st.success(f"{len(reinclude_idx)} row(s) added to Step 2 below.")

        render_review_and_post(rows, skipped_count, existing_or_numbers=posted_or_numbers)

    else:
        st.info("Upload the monthly dana list Excel file to get started.")

# ==============================================
# TAB 3 - Reconciliation
# ==============================================
with tab_recon:
    st.subheader("Reconciliation - Dana List / Bank Statement vs Autocount")
    st.caption("Verify every donation has been recorded in Autocount.")

    recon_col1, recon_col2 = st.columns([3, 1])
    with recon_col1:
        recon_source = st.radio("Reconcile using:", ["Dana List (Excel)", "Bank Statement (CSV)"], horizontal=True)

    @st.cache_data(ttl=300, show_spinner=False)
    def _fetch_posted_cached(from_d: str, to_d: str) -> list:
        """Cache Autocount OR fetch for 5 minutes so changing filters doesn't re-fetch."""
        return AutocountClient().get_posted_receipts(from_d, to_d)

    with recon_col2:
        if st.button("🔄 Refresh from Autocount", help="Clear cached data and pull the latest OR records from Autocount now"):
            _fetch_posted_cached.clear()
            st.success("Cache cleared - latest data will be fetched.")

    st.divider()

    def _render_recon_results(result_rows: list, source_label: str, ac_unmatched: list = None):
        df_result = pd.DataFrame(result_rows)
        found    = df_result["Status"].str.startswith("Found").sum()
        missing  = (df_result["Status"] == "MISSING").sum()
        mismatch = df_result["Status"].isin(["MISMATCH", "DUPLICATE OR"]).sum()
        total    = len(df_result)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric(f"Total in {source_label}", total)
        c2.metric("Found in Autocount", found)
        c3.metric("Missing in Autocount", missing,
                  delta=f"-{missing}" if missing else None, delta_color="inverse")
        c4.metric("Mismatch / Duplicate OR", mismatch,
                  delta=f"-{mismatch}" if mismatch else None, delta_color="inverse")

        if missing > 0:
            st.error(f"{missing} donation(s) from the {source_label} are NOT found in Autocount!")
        if mismatch > 0:
            st.warning(f"{mismatch} row(s) have a problem with their OR number - either the same OR number is used "
                       "by more than one row in the file, or the donor/amount doesn't match Autocount. Please check these manually.")
        if missing == 0 and mismatch == 0:
            st.success(f"All donations in the {source_label} are recorded in Autocount.")

        st.divider()

        with st.expander("What do the status results mean?"):
            st.markdown("""
| Status | Meaning |
|--------|---------|
| **Found** | The OR number exists in Autocount, and donor/amount match. Receipt is correctly recorded. |
| **Found (by date+amount)** | No OR number on this row, but a record with the same date and amount was found in Autocount. Likely already posted. |
| **DUPLICATE OR** | The same OR number appears on more than one row in this file. Only one receipt exists in Autocount for that number - the extra row(s) are flagged. Fix the OR number in the source file. |
| **MISMATCH** | The OR number exists in Autocount, but under a different donor name or amount. Check manually. |
| **MISSING** | This donation cannot be found in Autocount - it may have been skipped or not yet posted. Action required. |

**Colour guide:** ðŸŸ¢ Green = Found &nbsp;&nbsp; ðŸŸ¡ Yellow = Mismatch / Duplicate &nbsp;&nbsp; ðŸ"´ Red = Missing
            """)

        filter_opt = st.radio("Show:", ["All", "Missing only", "Mismatch only", "Found only"], horizontal=True, key="recon_filter")
        if filter_opt == "Missing only":
            df_show = df_result[df_result["Status"] == "MISSING"]
        elif filter_opt == "Mismatch only":
            df_show = df_result[df_result["Status"].isin(["MISMATCH", "DUPLICATE OR"])]
        elif filter_opt == "Found only":
            df_show = df_result[df_result["Status"].str.startswith("Found")]
        else:
            df_show = df_result

        def _row_color(row):
            if row["Status"] == "MISSING":
                return ["background-color: #f8d7da"] * len(row)
            if row["Status"] in ("MISMATCH", "DUPLICATE OR"):
                return ["background-color: #fff3cd"] * len(row)
            return ["background-color: #d4edda"] * len(row)

        st.dataframe(df_show.style.apply(_row_color, axis=1), use_container_width=True, hide_index=True)

        buf = io.BytesIO()
        df_show.to_excel(buf, index=False)
        label = {"All": "All", "Missing only": "Missing Only", "Mismatch only": "Mismatch Only", "Found only": "Found Only"}.get(filter_opt, "All")
        st.download_button(
            f"Download Reconciliation Report ({label})", buf.getvalue(),
            file_name=f"reconciliation_report_{label.lower().replace(' ','_')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        # Reverse direction: OR records in Autocount that no row in the file matched
        if ac_unmatched is not None:
            st.divider()
            st.subheader(f"In Autocount but NOT in {source_label}")
            if not ac_unmatched:
                st.success(f"Every Autocount OR in this date range was matched by a row in the {source_label}.")
            else:
                st.error(f"{len(ac_unmatched)} OR record(s) exist in Autocount but were NOT matched by any row "
                         f"in the {source_label}. These may be extra/duplicate postings, or rows missing from your file.")
                df_unmatched = pd.DataFrame(ac_unmatched).rename(columns={
                    "docNo": "OR Number", "date": "Date", "dealWith": "Donor Name", "amount": "Amount (RM)"
                }).sort_values("OR Number")
                st.dataframe(df_unmatched, use_container_width=True, hide_index=True)
                buf3 = io.BytesIO()
                df_unmatched.to_excel(buf3, index=False)
                st.download_button("Download Unmatched Autocount OR List", buf3.getvalue(),
                                   file_name="autocount_unmatched_or.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def _build_ac_lookups(posted: list):
        import re as _re2
        ac_or_numbers = set()
        ac_by_docno = {}  # docNo (and base docNo) -> record {dealWith, amount, date}
        for p in posted:
            doc = p["docNo"]
            base = _re2.sub(r"-\d+$", "", doc)
            ac_or_numbers.add(doc)
            ac_or_numbers.add(base)
            ac_by_docno.setdefault(doc, p)
            ac_by_docno.setdefault(base, p)
        ac_by_date_amount = {}
        for p in posted:
            key = (p["date"], round(p["amount"], 2))
            ac_by_date_amount.setdefault(key, []).append(p["docNo"])
        return ac_or_numbers, ac_by_date_amount, ac_by_docno

    # â"€â"€ Dana List reconciliation
    if recon_source == "Dana List (Excel)":
        recon_file = st.file_uploader("Upload Dana List Excel", type=["xlsx"], key="recon_dana_upload")

        if recon_file:
            try:
                df_recon, _ = load_dana_list(recon_file, skip_blank_gl=False)
            except Exception as e:
                st.error(f"Error reading dana list: {e}")
                st.stop()

            with st.spinner("Fetching OR records from Autocount..."):
                posted_r = _fetch_posted_cached(df_recon["date"].min(), df_recon["date"].max())

            ac_or_numbers, ac_by_date_amount, ac_by_docno = _build_ac_lookups(posted_r)

            # Pool of Autocount records reachable by each OR number: the exact docNo
            # AND its base (OR-2606153-1 is reachable via both "OR-2606153-1" and
            # "OR-2606153"). Each record can only be claimed ONCE across the whole run,
            # so split receipts (4 dana rows sharing one base OR) match one suffix each.
            import re as _re3
            _pool_by_or = {}
            for p in posted_r:
                doc  = p["docNo"]
                base = _re3.sub(r"-\d+$", "", doc)
                _pool_by_or.setdefault(doc, []).append(p)
                if base != doc:
                    _pool_by_or.setdefault(base, []).append(p)
            _claimed = set()

            def _claim_record(or_no, amount):
                """Claim unclaimed Autocount record(s) for this OR number.
                1. Exact single-amount match  -> claim that one
                2. Amount equals the SUM of all remaining records for this OR
                   (split receipt: one dana row = OR-xxx-1..-N in Autocount) -> claim all
                3. Otherwise claim one anyway (flagged as mismatch by caller)
                Returns (list_of_records, amount_matched)."""
                pool = [p for p in _pool_by_or.get(or_no, []) if id(p) not in _claimed]
                for p in pool:
                    if round(float(p["amount"]), 2) == amount:
                        _claimed.add(id(p))
                        return [p], True
                if len(pool) > 1 and round(sum(float(p["amount"]) for p in pool), 2) == amount:
                    for p in pool:
                        _claimed.add(id(p))
                    return pool, True
                if pool:
                    _claimed.add(id(pool[0]))
                    return [pool[0]], False
                return [], False

            # For rows without OR numbers: each Autocount record can only match once
            _remaining_da = {k: list(v) for k, v in ac_by_date_amount.items()}
            _consumed_docnos = set()

            result_rows = []
            for _, txn in df_recon.iterrows():
                or_no  = txn["or_number"]
                amount = round(float(txn["amount"]), 2)
                if or_no and or_no in _pool_by_or:
                    ac_recs, amt_ok = _claim_record(or_no, amount)
                    if not ac_recs:
                        # All Autocount records for this OR already claimed by earlier rows
                        status  = "DUPLICATE OR"
                        matched = f"{or_no} - more rows in file than receipts in Autocount"
                    elif amt_ok:
                        status  = "Found"
                        matched = ", ".join(p["docNo"] for p in ac_recs)
                    else:
                        ac_rec  = ac_recs[0]
                        status  = "MISMATCH"
                        matched = f"{ac_rec['docNo']} (Autocount: {ac_rec.get('dealWith','')}, RM {round(float(ac_rec.get('amount',0)),2):,.2f})"
                elif not or_no:
                    pool = _remaining_da.get((txn["date"], amount), [])
                    if pool:
                        matched = pool.pop(0)   # consume one Autocount record
                        _consumed_docnos.add(matched)
                        status  = "Found (by date+amount)"
                    else:
                        matched = ""
                        status  = "MISSING"
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

            # Autocount records that nothing in the dana list claimed
            ac_unmatched = [p for p in posted_r
                            if id(p) not in _claimed and p["docNo"] not in _consumed_docnos]

            _render_recon_results(result_rows, "Dana List", ac_unmatched=ac_unmatched)
        else:
            st.info("Upload the monthly dana list Excel file to run the reconciliation check.")

    # â"€â"€ Bank Statement reconciliation
    else:
        recon_csv = st.file_uploader("Upload Maybank Bank Statement (CSV or PDF)", type=["csv", "pdf"], key="recon_bank_upload")

        if recon_csv:
            import tempfile, os
            _suffix = ".pdf" if recon_csv.name.lower().endswith(".pdf") else ".csv"
            with tempfile.NamedTemporaryFile(delete=False, suffix=_suffix) as tmp:
                tmp.write(recon_csv.read())
                tmp_path = tmp.name
            try:
                df_bank = load_statement(tmp_path)
                os.unlink(tmp_path)
            except Exception as e:
                st.error(f"Error reading bank statement: {e}")
                st.stop()

            with st.spinner("Fetching OR records from Autocount..."):
                from_r   = df_bank["date"].min().strftime("%Y-%m-%d")
                to_r     = df_bank["date"].max().strftime("%Y-%m-%d")
                posted_r = _fetch_posted_cached(from_r, to_r)

            # Match bank rows to Autocount records in 3 passes so split receipts work:
            #   A. exact date+amount match against whole (non-suffixed) receipts
            #   B. one bank row = SUM of a suffixed group (OR-xxx-1..-N) on the same date
            #   C. exact date+amount match against leftover suffixed receipts
            # Each Autocount record can only be claimed once.
            import re as _re4

            def _base_of(doc):
                m = _re4.match(r"^(OR-\d{7})-\d+$", doc)
                return m.group(1) if m else None

            _by_da   = {}
            _groups  = {}
            for p in posted_r:
                _by_da.setdefault((p["date"], round(float(p["amount"]), 2)), []).append(p)
                b = _base_of(p["docNo"])
                if b:
                    _groups.setdefault((b, p["date"]), []).append(p)
            _claimed_docs = set()

            rows_info = []
            for _, txn in df_bank.iterrows():
                rows_info.append({
                    "date":    txn["date"].strftime("%Y-%m-%d"),
                    "amount":  round(float(txn["credit"]), 2),
                    "donor":   txn["donor_name"],
                    "matched": None,
                })

            # Pass A0 - whole receipts where the donor name ALSO matches (most precise)
            for r in rows_info:
                donor_key = str(r["donor"]).strip().upper()
                pool = [p for p in _by_da.get((r["date"], r["amount"]), [])
                        if p["docNo"] not in _claimed_docs and _base_of(p["docNo"]) is None
                        and donor_key and p.get("dealWith", "") == donor_key]
                if pool:
                    _claimed_docs.add(pool[0]["docNo"])
                    r["matched"] = pool[0]["docNo"]

            # Pass A - whole receipts by date+amount
            for r in rows_info:
                if r["matched"]:
                    continue
                pool = [p for p in _by_da.get((r["date"], r["amount"]), [])
                        if p["docNo"] not in _claimed_docs and _base_of(p["docNo"]) is None]
                if pool:
                    _claimed_docs.add(pool[0]["docNo"])
                    r["matched"] = pool[0]["docNo"]

            # Pass B - split receipt groups (sum of remaining suffixes == bank amount)
            for r in rows_info:
                if r["matched"]:
                    continue
                for (b, d), grp in _groups.items():
                    if d != r["date"]:
                        continue
                    un = [p for p in grp if p["docNo"] not in _claimed_docs]
                    if un and round(sum(float(p["amount"]) for p in un), 2) == r["amount"]:
                        for p in un:
                            _claimed_docs.add(p["docNo"])
                        r["matched"] = ", ".join(sorted(p["docNo"] for p in un))
                        break

            # Pass C - leftover suffixed receipts by exact date+amount
            for r in rows_info:
                if r["matched"]:
                    continue
                pool = [p for p in _by_da.get((r["date"], r["amount"]), [])
                        if p["docNo"] not in _claimed_docs]
                if pool:
                    _claimed_docs.add(pool[0]["docNo"])
                    r["matched"] = pool[0]["docNo"]

            result_rows = []
            for r in rows_info:
                result_rows.append({
                    "Status":         "Found (by date+amount)" if r["matched"] else "MISSING",
                    "Matched AC Doc": r["matched"] or "",
                    "Date":           r["date"],
                    "Donor Name":     r["donor"],
                    "Amount (RM)":    r["amount"],
                })

            # Autocount records that no bank transaction matched
            ac_unmatched_bank = [p for p in posted_r if p["docNo"] not in _claimed_docs]

            _render_recon_results(result_rows, "Bank Statement", ac_unmatched=ac_unmatched_bank)
        else:
            st.info("Upload the Maybank bank statement CSV to run the reconciliation check.")

# ==============================================
# TAB - Print Batch OR (lookup any date range)
# ==============================================
with tab_print:
    st.subheader("Print Batch OR — Lookup by Date Range")
    st.caption("Fetch OR numbers posted in Autocount for any period, to print in batch. "
               "Works for past postings too, not just the current session.")

    import datetime as _dt
    col_a, col_b = st.columns(2)
    with col_a:
        lookup_from = st.date_input("From Date", value=_dt.date.today().replace(day=1), key="lookup_from")
    with col_b:
        lookup_to = st.date_input("To Date", value=_dt.date.today(), key="lookup_to")

    if st.button("🔍 Fetch OR Numbers", type="primary"):
        if lookup_from > lookup_to:
            st.error("From Date must be before To Date.")
        else:
            with st.spinner("Fetching OR records from Autocount..."):
                lookup_client = AutocountClient()
                lookup_results = lookup_client.get_posted_receipts(
                    lookup_from.strftime("%Y-%m-%d"), lookup_to.strftime("%Y-%m-%d")
                )
            st.session_state["print_lookup_results"] = lookup_results

    # Optional: upload the dana list to add WhatsApp numbers (not stored in Autocount)
    print_dana_file = st.file_uploader(
        "Optional: upload Dana List Excel to include WhatsApp numbers",
        type=["xlsx"], key="print_dana_upload",
    )

    if "print_lookup_results" in st.session_state:
        lookup_results = st.session_state["print_lookup_results"]
        if not lookup_results:
            st.info("No OR records found for this date range.")
        else:
            df_lookup = pd.DataFrame(lookup_results).sort_values("docNo")
            df_lookup = df_lookup.rename(columns={
                "docNo": "OR Number", "date": "Date", "dealWith": "Donor Name", "amount": "Amount (RM)"
            })

            # Join WhatsApp numbers from the dana list by OR number (suffixed ORs
            # like OR-2606153-1 fall back to their base number OR-2606153)
            if print_dana_file:
                try:
                    df_pd, _ = load_dana_list(print_dana_file, skip_blank_gl=False)
                    import re as _re5

                    # Primary: match by OR number (column G of the dana list)
                    _mobile_by_or = {r["or_number"]: r.get("mobile", "")
                                     for _, r in df_pd.iterrows() if r["or_number"]}

                    # Fallback: match by date+amount+donor, then date+amount, for
                    # dana lists where the OR column was left blank
                    _mobile_by_dad = {}
                    _mobile_by_da  = {}
                    for _, r in df_pd.iterrows():
                        mob = r.get("mobile", "")
                        if mob:
                            amt = round(float(r["amount"]), 2)
                            donor_key = str(r["donor_name"]).strip().upper()
                            _mobile_by_dad.setdefault((r["date"], amt, donor_key), []).append(mob)
                            _mobile_by_da.setdefault((r["date"], amt), []).append(mob)

                    def _wa_lookup(row):
                        doc = row["OR Number"]
                        if doc in _mobile_by_or:
                            return _mobile_by_or[doc]
                        base = _re5.sub(r"-\d+$", "", doc)
                        if base in _mobile_by_or:
                            return _mobile_by_or[base]
                        amt = round(float(row["Amount (RM)"]), 2)
                        donor_key = str(row["Donor Name"]).strip().upper()
                        pool = _mobile_by_dad.get((row["Date"], amt, donor_key), [])
                        if pool:
                            return pool.pop(0)
                        pool = _mobile_by_da.get((row["Date"], amt), [])
                        return pool.pop(0) if pool else ""

                    df_lookup["WhatsApp Mobile"] = df_lookup.apply(_wa_lookup, axis=1)
                    _wa_count = (df_lookup["WhatsApp Mobile"] != "").sum()
                    st.info(f"WhatsApp numbers added for {_wa_count} of {len(df_lookup)} OR(s) from the dana list.")
                except Exception as e:
                    st.warning(f"Could not read dana list for WhatsApp numbers: {e}")
            else:
                st.info("💡 To include WhatsApp numbers, upload the dana list Excel in the box above — "
                        "Autocount does not store phone numbers, so they come from the dana list.")

            st.success(f"Found {len(df_lookup)} OR record(s) totalling RM {df_lookup['Amount (RM)'].sum():,.2f}")
            st.dataframe(df_lookup, use_container_width=True, hide_index=True)

            buf_x = io.BytesIO()
            df_lookup.to_excel(buf_x, index=False)
            st.download_button("Download OR Summary (Excel)", buf_x.getvalue(),
                               file_name="batch_or_summary.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            st.divider()
            st.markdown("""
Follow these steps in **Autocount Cloud** to print and save these receipts as one PDF:
1. Open **Cash Book Entry**
2. Click **Print Listing**
3. Select the OR numbers listed below (search/filter by OR number)
4. Tick all matching rows, click **Print**
5. Choose **Save as PDF** and store it in your receipts folder
            """)
            or_list_text = "\n".join(df_lookup["OR Number"].tolist())
            st.text_area(f"OR Numbers to Print ({len(df_lookup)} receipt(s)) - copy this list",
                        value=or_list_text, height=150)
            st.download_button("Download OR Number List (.txt)", or_list_text,
                               file_name="or_numbers_to_print.txt", mime="text/plain")

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
