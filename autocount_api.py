# autocount_api.py - Autocount Cloud API wrapper

import requests
from config_loader import AUTOCOUNT


class AutocountClient:
    def __init__(self):
        # Full base: https://accounting-api.autocountcloud.com/5260
        self.base_url = f"{AUTOCOUNT['base_url'].rstrip('/')}/{AUTOCOUNT['company_id']}"

    # ------------------------------------------------------------------ auth

    def _headers(self) -> dict:
        return {
            "Key-ID": AUTOCOUNT["key_id"],
            "API-Key": AUTOCOUNT["api_key"],
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        resp = requests.get(
            f"{self.base_url}/{path.lstrip('/')}",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        resp = requests.post(
            f"{self.base_url}/{path.lstrip('/')}",
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise Exception(f"HTTP {resp.status_code}: {detail}")
        return resp.json() if resp.text.strip() else {"status": "created", "httpStatus": resp.status_code}

    # --------------------------------------------------------- duplicate check

    def get_posted_receipts(self, from_date: str, to_date: str) -> list[dict]:
        """
        Return OR records whose docDate falls between from_date and to_date (YYYY-MM-DD).
        Scans from the last page backwards; stops after 5 consecutive pages with no matches.
        Returns list of dicts: {docNo, date, dealWith, amount}
        """
        page_size = 100
        r0 = self._get("/payment/listing", params={"page": 1, "pageSize": page_size, "docType": "OR"})
        total = r0.get("totalCount", r0.get("total", 0))
        actual = len(r0.get("data", [])) or page_size
        last_page = max(1, -(-total // actual))

        from_dt = from_date[:10]
        to_dt   = to_date[:10]
        results = []
        no_match_streak = 0

        for pg in range(last_page, max(0, last_page - 30), -1):
            r = self._get("/payment/listing", params={"page": pg, "pageSize": page_size, "docType": "OR"})
            found_any = False
            for d in r.get("data", []):
                m = d["master"]
                doc_date = m.get("docDate", "")[:10]
                if from_dt <= doc_date <= to_dt:
                    results.append({
                        "docNo":    m.get("docNo", ""),
                        "date":     doc_date,
                        "dealWith": (m.get("dealWith") or "").strip().upper(),
                        "amount":   float(m.get("totalPayment") or 0),
                    })
                    found_any = True
            if found_any:
                no_match_streak = 0
            else:
                no_match_streak += 1
                if no_match_streak >= 5:
                    break

        return results

    # --------------------------------------------------------------- customers

    def get_all_customers(self) -> list[dict]:
        """Returns list of debtor dicts from Autocount."""
        data = self._get("/debtor/listing", params={"page": 1, "pageSize": 1000})
        return data.get("data", []) if isinstance(data, dict) else data

    # --------------------------------------------------------------- invoices

    def get_outstanding_invoices(self, customer_code: str) -> list[dict]:
        """Returns outstanding invoices for a customer."""
        import datetime
        today = datetime.date.today().isoformat()
        data = self._get(
            "/knockoffentry/outstandingtransactions",
            params={"accNo": customer_code, "docDate": today},
        )
        records = data if isinstance(data, list) else data.get("data", [])
        return records

    # --------------------------------------------------------------- receipts

    def get_last_or_number(self, prefix: str = "") -> str | None:
        """
        Return the highest OR doc number matching prefix (e.g. 'OR-2606').
        Gets total count, jumps to the last page, scans up to 10 pages backward.
        Uses only ~11 API calls maximum.
        """
        page_size = 100
        r0 = self._get("/payment/listing", params={"page": 1, "pageSize": page_size, "docType": "OR"})
        total = r0.get("totalCount", r0.get("total", 0))
        actual_per_page = len(r0.get("data", [])) or page_size
        last_page = max(1, -(-total // actual_per_page))

        best = None
        no_match_after_found = 0
        for pg in range(last_page, max(0, last_page - 15), -1):
            r = self._get("/payment/listing", params={"page": pg, "pageSize": page_size, "docType": "OR"})
            docs = [d["master"]["docNo"] for d in r.get("data", [])
                    if d["master"]["docNo"].startswith(prefix if prefix else "OR-")]
            if docs:
                candidate = sorted(docs)[-1]
                if best is None or candidate > best:
                    best = candidate
                no_match_after_found = 0
            else:
                if best:
                    no_match_after_found += 1
                    if no_match_after_found >= 3:
                        break
        return best

    def _next_or_doc_no(self, receipt_date: str, offset: int = 0) -> str:
        """
        Build the next OR doc number based on the last one in Autocount.
        Format: OR-YYMMNNN  e.g. OR-2606002
        receipt_date: "YYYY-MM-DD"
        offset: add extra increment to skip already-used numbers
        """
        yy = receipt_date[2:4]
        mm = receipt_date[5:7]
        prefix = f"OR-{yy}{mm}"

        last = self.get_last_or_number(prefix=prefix)
        if last and last.startswith(prefix):
            seq = int(last[len(prefix):]) + 1 + offset
        else:
            seq = 1 + offset

        return f"{prefix}{seq:03d}"

    def check_doc_no_exists(self, doc_no: str) -> bool:
        """Check whether an OR document number already exists in Autocount."""
        try:
            r = self._get("/payment/listing", params={"page": 1, "pageSize": 1, "docType": "OR", "docNo": doc_no})
            return bool(r.get("data"))
        except Exception:
            return False

    def create_donation_receipt(
        self,
        receipt_date: str,      # "YYYY-MM-DD"
        amount: float,
        bank_gl_code: str,      # Dr: bank account e.g. "310-1000"
        donation_gl_code: str,  # Cr: donation income e.g. "500-4000"
        donor_name: str,
        payment_method: str = "BANK",
        description: str = "",
        department: str = "",
        doc_no: str = "",
        strict_doc_no: bool = False,
    ) -> dict:
        """
        Post an Official Receipt (OR) via Cash Book Entry:
            Dr  Bank Account (paymentDetails)
            Cr  Donation Income GL (details)

        If strict_doc_no=True and doc_no is provided, only that exact number is attempted -
        no silent fallback to the next available number. Raises on duplicate/failure instead.
        """
        # Use provided doc_no, or auto-detect next available
        last_err = None
        provided = doc_no.strip() if doc_no else ""
        max_attempts = 1 if (provided and strict_doc_no) else 20
        for attempt in range(max_attempts):
            doc_no = provided if (provided and attempt == 0) else self._next_or_doc_no(receipt_date, offset=attempt)
            payload = {
                "master": {
                    "docType": "OR",
                    "docNo": doc_no,
                    "docDate": f"{receipt_date}T00:00:00",
                    "currencyCode": "MYR",
                    "currencyRate": 1,
                    "journalType": "BANK",
                    "dealWith": donor_name,
                    "description": description or donor_name,
                },
                "details": [
                    {
                        "accNo": donation_gl_code,
                        "description": description or donor_name,
                        "amount": amount,
                        **({"deptNo": department} if department else {}),
                    }
                ],
                "paymentDetails": [
                    {
                        "paymentMethod": payment_method,
                        "paymentBy": "IBG",
                        "paymentAmt": amount,
                    }
                ],
            }
            try:
                result = self._post("/payment", payload)
                result["docNo"] = doc_no
                return result
            except Exception as e:
                if "Duplicate document number" in str(e):
                    last_err = e
                    continue  # try next number
                raise  # other errors bubble up
        raise Exception(f"Could not find a free OR doc number after 20 attempts. Last error: {last_err}")
