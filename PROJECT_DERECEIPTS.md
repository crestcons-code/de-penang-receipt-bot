# PROJECT_DERECEIPTS.md
# DE Penang — Autocount Donation Receipt Automation
# Persatuan Dhamma Malaysia (Malaysia Dhamma Society - Penang Branch)

---

## Purpose
Automate posting of Official Receipts (OR) into Autocount Cloud by parsing Maybank bank statements. All incoming bank payments are donations — there are no invoices or debtors involved.

---

## Architecture

```
Maybank CSV/PDF
      │
      ▼
parse_maybank.py       ← extracts donor name + GL-matching text from 3 columns
      │
      ▼
donation_mapping.py    ← keyword match → GL account code + department
      │
      ▼
prepare.py / app.py    ← dedup check against Autocount, assign OR numbers, build review
      │
      ▼
[User reviews & edits in Excel or Streamlit web app]
      │
      ▼
main.py / app.py       ← posts OR entries to Autocount Cloud via REST API
```

**Two workflows:**
1. **CLI** — `prepare.py` → edit Excel on Desktop → `main.py`
2. **Web app** — `app.py` (Streamlit at `http://localhost:8501`)

---

## Folder Structure

```
autocount-receipt-bot/
├── app.py               Streamlit web app (primary UI)
├── main.py              CLI Step 2: post reviewed Excel to Autocount
├── prepare.py           CLI Step 1: parse CSV → review Excel on Desktop
├── parse_maybank.py     Maybank CSV/PDF parser
├── donation_mapping.py  GL code + department keyword mapping
├── autocount_api.py     Autocount Cloud REST API wrapper
├── config.py            Credentials & settings (never commit to git)
├── requirements.txt     pandas, openpyxl, pdfplumber, rapidfuzz, requests
└── PROJECT_DERECEIPTS.md  ← this file
```

---

## Autocount Cloud API

| Item | Value |
|------|-------|
| Base URL | `https://accounting-api.autocountcloud.com` |
| Company ID | `5260` |
| Full base | `https://accounting-api.autocountcloud.com/5260` |
| Auth headers | `Key-ID: 49bbce7e-...` and `API-Key: 48615875-...` |
| Rate limit | **100 requests / minute** |
| OR endpoint | `POST /payment` |
| OR listing | `GET /payment/listing?page=N&pageSize=100&docType=OR` |

**Key API facts:**
- `pageSize` is capped at 100 by the server regardless of what you pass
- Date filters (`fromDate`/`toDate`) on listing do **not** work — must filter client-side
- Total OR records ≈ 15,800+ — June 2026 entries are on pages ~154–159
- Empty 201 response body is normal on successful OR creation

**OR payload structure:**
```json
{
  "master": {
    "docType": "OR",
    "docNo": "OR-2606293",
    "docDate": "2026-06-18T00:00:00",
    "currencyCode": "MYR",
    "currencyRate": 1,
    "journalType": "BANK",
    "dealWith": "DONOR NAME",
    "description": "TCM"
  },
  "details": [{
    "accNo": "500-8000",
    "description": "TCM",
    "amount": 100.00,
    "deptNo": "TCM"
  }],
  "paymentDetails": [{
    "paymentMethod": "BANK",
    "paymentBy": "IBG",
    "paymentAmt": 100.00
  }]
}
```

---

## OR Numbering

- Format: `OR-YYMMNNN` e.g. `OR-2606293` (June 2026, sequence 293)
- Numbers are pre-assigned in the review file before posting
- `autocount_api.get_last_or_number(prefix="OR-2606")` scans last pages of listing to find highest existing number
- On duplicate error, retries with next sequential number (up to 20 attempts)
- **Do not use `docNoFormatName`** — causes duplicate conflicts; always specify explicit `docNo`

---

## Maybank CSV Format

- Download from Maybank2u, `skiprows=3`, encoding `utf-8-sig`
- Columns used:

| Column | Usage |
|--------|-------|
| `Transaction Date` | Date of transaction |
| `Transaction Description 1` | **Primary GL keyword source** (donation purpose) |
| `Transaction Description 2` | Secondary GL keyword source |
| `Beneficiary/ Biller Name` | Donor name for Autocount `dealWith` field |
| `Transaction Amount: Cash-in (RM)` | Amount |

- GL matching uses `desc1 + desc2 + beneficiary` combined text
- Donor name (for Autocount) uses beneficiary name only, asterisk stripped

---

## GL Account Mapping

File: `donation_mapping.py` — edit keywords here, no other files need changing.

Key accounts:
| GL Code | Description | Department |
|---------|-------------|-----------|
| 500-4000 | General Donation (fallback) | — |
| 500-5004 | Kathina | KATHINA |
| 500-5010 | Q-Sun | Q-SUN |
| 500-6000 | Monk & Nun Requisites | — |
| 500-7000 | SP Meditation Point | SP |
| 500-8000 | TCM | TCM |
| 500-9000 | Paritta Group | PARITTA |
| 500-9001 | Parami Group | PARAMI |
| 500-9002 | Mahadana | MAHADANA |
| 500-9003 | Mangala Family | MANGALA |
| 500-9005 | Tree House | — |

Bank GL: `310-1000` (CASH AT BANK - MAYBANK)

---

## Business Rules

1. **All receipts are donations** — no invoice knock-off, no debtor matching
2. **journalType must be `BANK`** — produces OR- prefix; using CASH produces COR- prefix
3. **Duplicate detection** — before building review, fetch existing ORs for the date range and exclude transactions already in Autocount (matched by date + amount)
4. **Department** — populated automatically from GL code mapping; blank for GL codes with no department
5. **Description in Autocount** = short donation type (e.g. "TCM", "Tree House"), NOT the GL account description
6. **DUITNOW QR-** payments with no description → General Donation (500-4000), user reviews manually
7. **Veranda** → Tree House (500-9005); **Robe** → Monk & Nun Requisites (500-6000)

---

## Config (`config.py`)

```python
AUTOCOUNT = {
    "base_url": "https://accounting-api.autocountcloud.com",
    "key_id":   "49bbce7e-6c99-4a93-b3f4-dbcb8410ee7a",
    "api_key":  "48615875-e412-4bd5-8a93-e7c0df5db103",
    "company_id": "5260",
}
MAYBANK_GL_CODE = "310-1000"
DEFAULT_PAYMENT_METHOD = "BANK"
```

⚠️ **Security**: `config.py` contains live API credentials. Do not commit to git or share.
User should regenerate API keys from Autocount API Keys page when possible.

---

## Review Excel Template Columns

| Column | Editable | Notes |
|--------|----------|-------|
| OR Number | No | Pre-assigned, sequential from last in Autocount |
| Post (YES/NO) | Yes | Change to NO to skip |
| Date | No | From bank statement |
| Donor Name | No | From beneficiary name |
| GL Account Code | Yes | Change if auto-mapping wrong |
| GL Description | No | Auto from GL code |
| Description (in Autocount) | Yes | Short type e.g. "TCM" |
| Department | Yes | Auto from GL; blank if none |
| Amount (RM) | No | From bank statement |
| **TOTAL row** | — | Yellow highlighted, bold, bottom of sheet |

---

## Web App (`app.py`)

Run: `python -m streamlit run app.py` → opens at `http://localhost:8501`

Features:
- Upload Maybank CSV or PDF
- Auto-dedup against Autocount (shows warning if transactions already posted)
- Pre-assigned OR numbers shown in table
- **✔ / ✘ buttons** above Post column to tick/untick all rows
- GL Account dropdown, editable Description and Department per row
- 3 metric boxes: Transactions / Selected / Total Amount (RM)
- Post button with progress bar, per-row success/error display
- Download posting report as Excel

---

## Remaining Work

- [ ] **PDF statement support** — `parse_pdf()` exists but untested with real Maybank PDF format
- [ ] **Multi-month statements** — OR numbering assumes single month; if statement spans two months, prefix logic needs review
- [ ] **Duplicate detection by donor name** — current dedup uses date+amount only; two donors sending same amount on same day would be incorrectly skipped
- [ ] **Streamlit: OR number refresh** — if user changes a GL account dropdown, OR numbers don't re-check; they are frozen at upload time
- [ ] **Error recovery** — if posting fails mid-batch, already-posted rows have no rollback; user must check Autocount manually
- [ ] **API key rotation** — live credentials in `config.py`; user advised to regenerate from Autocount dashboard
- [ ] **Test files cleanup** — `probe_docno.py`, `test_api.py`, `test_path.py`, `match_customers.py` are debug/test scripts; can be deleted
- [ ] **MGC department** — "Mangala Growth Center" appears in Departments.xlsx but has no GL mapping yet
- [ ] **Posting report** — currently saved to Desktop; consider a configurable output folder

---

## How to Run (Quick Reference)

```bash
# Install dependencies (first time)
pip install -r requirements.txt
pip install streamlit

# Web app (recommended)
cd "C:\Users\SerVer2\Documents\Claude ai\autocount-receipt-bot"
python -m streamlit run app.py

# CLI workflow
python prepare.py "bank_statement.csv"   # → review_YYYYMMDD.xlsx on Desktop
# edit Excel, then:
python main.py "C:\Users\...\review_YYYYMMDD.xlsx"
```
