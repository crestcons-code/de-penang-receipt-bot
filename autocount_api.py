# autocount_api.py - Autocount Cloud API wrapper

import calendar
import re
import time
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
        # Retry on rate limits (429) and transient server errors (5xx) instead of
        # letting a single blip crash the whole app - GET requests are read-only
        # and safe to repeat.
        last_err = None
        for attempt in range(4):
            resp = requests.get(
                f"{self.base_url}/{path.lstrip('/')}",
                headers=self._headers(),
                params=params,
                timeout=15,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = requests.exceptions.HTTPError(
                    f"HTTP {resp.status_code} from Autocount ({path})", response=resp
                )
                if attempt < 3:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise last_err
            resp.raise_for_status()
            return resp.json()
        raise last_err

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

        Uses the listing endpoint's own startDate/endDate filter and walks every
        page of the result. An earlier version binary-searched the unfiltered
        listing on the assumption it was roughly date-ordered, then read a couple
        of pages either side. It is not: the listing runs in creation order, so a
        receipt dated the 1st but entered on the 6th sits pages away from its
        neighbours. That silently dropped 26 of March's 718 receipts - and
        anything missed here looks UNPOSTED to the duplicate check, which is how
        already-receipted donations reappeared in Step 2 ready to be posted twice.

        Returns list of dicts: {docNo, date, dealWith, amount}
        """
        from_dt, to_dt = from_date[:10], to_date[:10]
        results, page = [], 1
        while True:
            r = self._get("/payment/listing", params={
                "page": page, "docType": "OR",
                "startDate": from_dt, "endDate": to_dt,
            })
            data = r.get("data", [])
            if not data:
                break
            for d in data:
                m = d["master"]
                doc_date = (m.get("docDate") or "")[:10]
                # Trust but verify the server-side filter
                if not (from_dt <= doc_date <= to_dt):
                    continue
                results.append({
                    "docNo":    m.get("docNo", ""),
                    "date":     doc_date,
                    "dealWith": (m.get("dealWith") or "").strip().upper(),
                    "amount":   float(m.get("totalPayment") or 0),
                    "cancelled": bool(m.get("cancelled")),
                })
            page += 1
            # A month of receipts is a few hundred; this only stops a runaway loop
            # if the API ever ignores the page parameter.
            if page > 200:
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
        Return the highest OR doc number matching prefix (e.g. 'OR-2602').

        Looks the month up by DATE. The previous version scanned the last 15
        pages of the unfiltered listing hoping the prefix appeared there - but
        that listing runs in creation order, so any month entered a while ago is
        nowhere near the end and it returned None. The caller then assumed the
        month was empty and started numbering at 001, proposing OR numbers that
        Autocount had already issued.
        """
        m = re.match(r"^OR-(\d{2})(\d{2})$", (prefix or "").strip())
        if not m:
            return None
        year = 2000 + int(m.group(1))
        month = int(m.group(2))
        first = f"{year:04d}-{month:02d}-01"
        last_day = calendar.monthrange(year, month)[1]
        last = f"{year:04d}-{month:02d}-{last_day:02d}"

        best_num, best_doc = -1, None
        for rec in self.get_posted_receipts(first, last):
            doc = rec.get("docNo", "")
            if not doc.startswith(prefix):
                continue
            # A reissued receipt can carry a suffix (OR-2602174-1); the sequence
            # number is what matters for deciding the next one.
            mm = re.match(rf"^{re.escape(prefix)}(\d{{3}})", doc)
            if mm and int(mm.group(1)) > best_num:
                best_num, best_doc = int(mm.group(1)), f"{prefix}{mm.group(1)}"
        return best_doc

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
        detail_lines: list = None,
    ) -> dict:
        """
        Post an Official Receipt (OR) via Cash Book Entry:
            Dr  Bank Account (paymentDetails)
            Cr  Donation Income GL (details)

        If strict_doc_no=True and doc_no is provided, only that exact number is attempted -
        no silent fallback to the next available number. Raises on duplicate/failure instead.

        detail_lines: optional list of {"description", "amount"} dicts. When given
        (multi-donor receipts), each becomes its own detail line on the same GL code;
        their amounts must sum to `amount`. Otherwise a single detail line is used.
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
                    # Autocount limits: DealWith max 100 chars, master Description max 80
                    "dealWith": donor_name[:100],
                    "description": (description or donor_name)[:80],
                },
                "details": (
                    [
                        {
                            "accNo": donation_gl_code,
                            "description": str(d["description"])[:100],   # detail Description max 100
                            "amount": d["amount"],
                            **({"deptNo": department} if department else {}),
                        }
                        for d in detail_lines
                    ]
                    if detail_lines else
                    [
                        {
                            "accNo": donation_gl_code,
                            "description": (description or donor_name)[:100],
                            "amount": amount,
                            **({"deptNo": department} if department else {}),
                        }
                    ]
                ),
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
