# slip_extractor.py - AI extraction of donation info from a bank-in slip image
# + the donor's accompanying WhatsApp text, into structured dana list fields.

import base64
import json
import re
import anthropic

MODEL = "claude-haiku-4-5-20251001"

EXTRACTION_PROMPT = """You are helping a Malaysian Buddhist temple's accounts team process donation \
records. You are given a bank transfer ("bank-in") slip image and the WhatsApp message text the \
donor sent alongside it (donor name(s), purpose, sometimes multiple donors sharing one transfer).

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
  "description": "purpose/reason for donation if mentioned (e.g. Kathina, Paritta, TCM, General)",
  "mobile": "012-3456789 or empty string if not stated",
  "confidence": "high|medium|low",   // your confidence that date+amount were read correctly
  "notes": "anything unclear or that needs human review, empty string if none"
}

If the slip image is blurry/unclear on date or amount, still give your best reading and set
confidence to "low" and explain in notes. If the WhatsApp text gives a donor name but the slip
shows a different name (e.g. slip shows the payer's bank name, WhatsApp gives the actual donor),
prefer the WhatsApp-stated donor name for "donors" - the bank name alone belongs in the slip
image's sender, not necessarily the donor being credited.
"""


def _get_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key)


def extract_donation(api_key: str, image_bytes: bytes, image_media_type: str,
                     whatsapp_text: str) -> dict:
    """
    Call Claude to read one slip image + its paired WhatsApp text.
    Returns the parsed dict (see EXTRACTION_PROMPT for shape), or raises on failure.
    """
    client = _get_client(api_key)
    image_b64 = base64.b64encode(image_bytes).decode()

    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": image_media_type,
                                                 "data": image_b64}},
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
