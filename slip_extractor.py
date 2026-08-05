# slip_extractor.py - AI extraction of donation info from a bank-in slip image
# + the donor's accompanying WhatsApp text, into structured dana list fields.

import base64
import json
import re
import anthropic

MODEL = "claude-haiku-4-5-20251001"

EXTRACTION_PROMPT = """You are helping a Malaysian Buddhist temple's accounts team process donation \
records. You are given a bank transfer ("bank-in") slip (image or PDF, from any Malaysian bank - \
Maybank, Hong Leong, Public Bank, CIMB etc. all use different layouts) and the WhatsApp message \
text the donor sent alongside it (donor name(s), purpose, sometimes multiple donors sharing one \
transfer).

Extract the following and return ONLY a single JSON object, no other text:

{
  "date": "YYYY-MM-DD",              // transaction date from the slip
  "amount": 100.00,                   // total transaction amount, numeric
  "donors": [
    {"name": "Donor Name", "amount": 100.00}   // one entry per donor; if only one donor,
                                                 // still use this list format with one item.
                                                 // If the WhatsApp text lists multiple donors
                                                 // with their own amounts (e.g. "1. Tan Ah Kow RM50,
                                                 // 2. Lim Ah Lian RM50"), split them out here -
                                                 // amounts must sum to the total "amount" above.
  ],
  "description": "purpose/reason for donation, INCLUDING any group/reference code shown on the
                   slip such as 'Recipient Reference' or 'Payment Details' (e.g. slip shows
                   'RC G17 BML Kathina' and 'road construction' -> description should be
                   'Kathina - Road Construction RC G17', preserving the RC G### code verbatim -
                   these group codes identify which donor group a Forest Monastery donation
                   belongs to and MUST NOT be dropped)",
  "mobile": "012-3456789 or empty string if not stated",
  "confidence": "high|medium|low",   // your confidence that date+amount were read correctly
  "notes": "anything unclear or that needs human review, empty string if none"
}

If the slip is blurry/unclear on date or amount, still give your best reading and set
confidence to "low" and explain in notes. If the WhatsApp text gives a donor name but the slip
shows a different name (e.g. slip shows the payer's bank name, WhatsApp gives the actual donor),
prefer the WhatsApp-stated donor name for "donors" - the bank name alone belongs in the slip's
sender field, not necessarily the donor being credited.

NEVER GUESS THE DATE. Use only a date actually printed on the slip as the transaction date.
Do NOT derive it from a file name, reference number or document ID - a reference like
"MY-230922783" is not a date, and inventing "2023-09-22" from it produces a receipt that can
never be matched to the bank statement. If the slip shows no readable date, return "" for date
and set confidence to "low"; a missing date is recoverable, a wrong one is not.

PHONE NUMBERS - only ever return a number that clearly belongs to the DONOR. The image may be a
screenshot of a WhatsApp conversation that includes the temple's OWN reply, which typically
carries a contact number ("Please contact 01X-XXXXXXX for any enquiries"), a signature, or the
temple's name. Never take a number from the temple's side of the conversation - it would be
filed as the donor's number and the receipt would never reach them. If the only number visible
belongs to the temple or you are unsure whose it is, return "".

IMPORTANT - group transfers: the slip's own "Payee reference" / "Other transfer details" field
often just carries a group label or collector's name (e.g. "RC G13 Kam Loong group for road
construction") for a transfer that bundles MANY individual donors' contributions together. When
the WhatsApp text separately lists individual named donors with their own amounts (numbered or
not, e.g. "1. Tan Ah Kow RM50" / "2. Lim Ah Lian RM50"), you MUST use that full donor breakdown
for "donors" - do NOT collapse them into the slip's single group label as if it were one donor.
The group label/reference code belongs in "description" (verbatim, per the RC G### rule above),
never as a substitute for the individual donor list. Only fall back to a single group-name donor
entry if the WhatsApp text gives no individual names at all.
"""


def _get_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key)


def extract_donation(api_key: str, file_bytes: bytes, media_type: str,
                     whatsapp_text: str) -> dict:
    """
    Call Claude to read one slip (image or PDF) + its paired WhatsApp text.
    media_type: "image/jpeg", "image/png", or "application/pdf".
    Returns the parsed dict (see EXTRACTION_PROMPT for shape), or raises on failure.
    """
    client = _get_client(api_key)
    file_b64 = base64.b64encode(file_bytes).decode()
    block_type = "document" if media_type == "application/pdf" else "image"

    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": block_type, "source": {"type": "base64", "media_type": media_type,
                                                     "data": file_b64}},
                    {"type": "text", "text": f"WhatsApp message text from donor:\n{whatsapp_text or '(none provided)'}\n\n{EXTRACTION_PROMPT}"},
                ],
            }
        ],
    )

    text = message.content[0].text.strip()
    # Model may wrap the JSON in a code fence despite instructions - strip it defensively
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"Could not find JSON in model response: {text[:200]}")
    data = json.loads(m.group(0))

    # Basic shape validation so callers can trust the structure
    data.setdefault("donors", [])
    data.setdefault("description", "")
    data.setdefault("mobile", "")
    data.setdefault("confidence", "low")
    data.setdefault("notes", "")
    return data
