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
  "notes": "anything unclear or that needs human review, empty string if none",
  "source_kind": "bank_slip | chat_screenshot | other",
      // "bank_slip"        = the bank's own transfer receipt/confirmation (any bank).
      // "chat_screenshot"  = a screenshot of a WhatsApp/messaging conversation. These
      //                      show messages, a contact name at the top, chat bubbles.
      //                      A chat screenshot may CONTAIN a small preview of a slip -
      //                      it is still a chat_screenshot.
      // "other"            = anything else.
  "attachment_filename": "..."
      // ONLY for chat_screenshot: if the conversation shows a file attachment, copy its
      // displayed file name EXACTLY as shown (e.g. "CIMB OCTO MY-230922783.pdf").
      // This is how a screenshot gets linked to the slip file it refers to, so copy it
      // character for character and do not tidy or shorten it. "" if none is visible.
}

If the slip is blurry/unclear on date or amount, still give your best reading and set
confidence to "low" and explain in notes. If the WhatsApp text gives a donor name but the slip
shows a different name (e.g. slip shows the payer's bank name, WhatsApp gives the actual donor),
prefer the WhatsApp-stated donor name for "donors" - the bank name alone belongs in the slip's
sender field, not necessarily the donor being credited.

YOU MAY BE GIVEN SEVERAL ATTACHMENTS FOR ONE DONATION - typically the bank slip itself plus a
screenshot of the WhatsApp conversation where the donor gives their name (donors often send the
slip and their name as two separate messages). Treat them together as evidence for a SINGLE
donation, never as separate donations, and return one JSON object covering all of them. Each
source is trusted for different things:
  - the BANK SLIP is authoritative for the transaction date and the amount
  - the CONVERSATION or message text is authoritative for the donor name(s) and the purpose
A chat screenshot does not show the transaction date (it shows when the message was sent, or just
"Today"), so never take the date from it.

NEVER RETURN THE RECIPIENT AS THE DONOR. Every slip shows the temple as the party being paid -
"PERSATUAN DHAMMA MALAYSIA", "Dhamma Earth Penang", or a Maybank account like 507013883446 /
5-0701-388344-6. That is who RECEIVED the money, and it is never the donor.

Look for the donor in this order:
  1. a name given in the WhatsApp message or conversation screenshot. In a GROUP chat every
     incoming message carries its sender's name above it, usually in colour (e.g. "Poey Hang Ai"
     above a message containing a slip). That sender IS the donor for the file they sent - use
     it even when the message text adds nothing but a greeting or repeats the name. Messages on
     the temple's own side are replies, not donations;
  2. the payer/sender/"From" name on the slip;
  3. the free-text reference the donor typed - fields called "Recipient Reference", "Payment
     Details", "Payment Reference" or similar. Donors here routinely put their own name there
     (e.g. Recipient Reference "Ooi soo yee" means the donor is Ooi soo yee), so use it when it
     reads like a person's or family's name.
If none of those yields a name, return an empty "donors" list rather than falling back to the
recipient - a blank donor prompts a volunteer to fill it in, whereas the temple's own name on a
receipt is a mistake that gets printed and posted out.

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


def read_attachment_names(api_key: str, file_bytes: bytes, media_type: str) -> list:
    """
    Ask one narrow question: which file attachment does this chat screenshot show?

    The main extraction already reports this, but inconsistently - it is one field
    among a dozen and gets missed. Pairing a screenshot to its slip depends on it,
    so screenshots that come back without one are asked again on their own, where
    the answer is far more reliable.

    Returns a LIST - a screenshot can show more than one attachment, and long
    digit runs in bank file names are often misread, so the caller matches these
    against the real file names fuzzily rather than trusting them literally.
    """
    client = _get_client(api_key)
    block_type = "document" if media_type == "application/pdf" else "image"
    msg = client.messages.create(
        model=MODEL,
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": [
                {"type": block_type, "source": {"type": "base64", "media_type": media_type,
                                                "data": base64.b64encode(file_bytes).decode()}},
                {"type": "text", "text":
                    "This is a screenshot of a messaging app. List the file name of EVERY file "
                    "attachment shown (a PDF or image card with a file name under or beside it), "
                    "exactly as displayed, including the extension - one per line, nothing else. "
                    "If no attached file name is visible, reply with only: NONE"},
            ],
        }],
    )
    out = msg.content[0].text.strip()
    if not out or out.upper().startswith("NONE"):
        return []
    names = []
    for line in out.splitlines():
        line = line.strip().strip('"').lstrip("-*• ").strip()
        # A real file name is short and carries an extension; anything else is
        # the model explaining itself, which must not reach the matcher.
        if line and "." in line and len(line) <= 120:
            names.append(line)
    return names


def extract_donation(api_key: str, file_bytes: bytes, media_type: str,
                     whatsapp_text: str) -> dict:
    """
    Call Claude to read one slip (image or PDF) + its paired WhatsApp text.
    media_type: "image/jpeg", "image/png", or "application/pdf".
    Returns the parsed dict (see EXTRACTION_PROMPT for shape), or raises on failure.
    """
    return extract_donation_multi(
        api_key, [{"bytes": file_bytes, "media_type": media_type, "name": ""}], whatsapp_text)


def extract_donation_multi(api_key: str, files: list, whatsapp_text: str,
                           primary_name: str = "") -> dict:
    """
    Read ONE donation that may be spread across several attachments - typically
    the bank slip plus a screenshot of the donor's WhatsApp message naming them.

    files: list of {"bytes": b"...", "media_type": "image/jpeg"|"image/png"|
                    "application/pdf", "name": "optional filename"}
    primary_name: file name of the bank slip this call is about. Worth passing
    whenever a screenshot is included: group-chat screenshots routinely show
    several people's donations at once, and without knowing which attachment is
    being asked about the model cannot tell whose message belongs to this slip -
    it then (correctly, but unhelpfully) returns no donor at all.

    All files are treated as evidence for a SINGLE donation, not several.
    """
    if not files:
        raise ValueError("No files given to extract_donation_multi")

    client = _get_client(api_key)
    content = []
    for i, f in enumerate(files, start=1):
        media_type = f["media_type"]
        block_type = "document" if media_type == "application/pdf" else "image"
        label = f["name"] or f"file {i}"
        # Naming each attachment lets the model refer to them in its notes, and
        # makes the "slip is authoritative for the date" rule easier to apply.
        content.append({"type": "text", "text": f"Attachment {i}: {label}"})
        content.append({
            "type": block_type,
            "source": {"type": "base64", "media_type": media_type,
                       "data": base64.b64encode(f["bytes"]).decode()},
        })

    content.append({
        "type": "text",
        "text": f"WhatsApp message text from donor:\n{whatsapp_text or '(none provided)'}\n\n{EXTRACTION_PROMPT}",
    })

    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
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
    data.setdefault("source_kind", "other")
    data.setdefault("attachment_filename", "")
    # A donor entry with a blank name is noise - it renders as " RM50" in the
    # review table and can't be receipted to anyone.
    data["donors"] = [d for d in data["donors"] if str(d.get("name", "")).strip()]
    return data
