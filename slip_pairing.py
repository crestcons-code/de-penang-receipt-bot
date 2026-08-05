# slip_pairing.py - work out which chat screenshot belongs to which bank slip.
#
# Donors usually send the slip and their name as two separate WhatsApp messages,
# so a volunteer's Drive folder ends up holding both, in no dependable order.
# Asking them to tick pairs by hand doesn't scale to a big batch, so this pairs
# them automatically from what each file actually contains.
#
# Deliberately conservative: a WRONG pairing puts one donor's name on another
# donor's payment, which produces a wrong receipt and is far worse than leaving
# a file unpaired for someone to look at. So each file is only paired on strong
# evidence, and anything ambiguous is left alone and reported.
#
# Pairing uses the attachment file name a screenshot displays, and nothing else.
# Matching on the AMOUNT was tried and dropped: the figure read off a chat rarely
# equals the slip's. On real samples a screenshot reported RM58 against a RM88
# slip, and RM50,000 against a RM30,000 one, because conversations mention other
# sums, pledges and running totals. Amount matching therefore pairs confidently
# and wrongly, which is the one outcome worth avoiding.

import re

from rapidfuzz import fuzz


def _norm_name(s: str) -> str:
    """Filenames are compared loosely - screenshots render them with odd spacing."""
    return re.sub(r"[\s_\-]+", "", str(s or "")).lower()


def _amount(r: dict):
    try:
        a = float(r.get("amount") or 0)
    except (TypeError, ValueError):
        return None
    return round(a, 2) if a > 0 else None


def pair_slips(results: list) -> tuple:
    """
    results: one extraction dict per file, each with at least
             _index, _name, source_kind, attachment_filename, amount, date, donors.

    Returns (groups, notes):
      groups: list of {"slip": result|None, "screenshots": [result, ...]}
              in the original file order, one entry per donation.
      notes:  list of human-readable strings explaining what was paired and why,
              so a volunteer can sanity-check the automatic decisions.
    """
    slips = [r for r in results if r.get("source_kind") == "bank_slip"]
    shots = [r for r in results if r.get("source_kind") == "chat_screenshot"]
    others = [r for r in results if r.get("source_kind") not in ("bank_slip", "chat_screenshot")]

    paired_to = {}        # id(screenshot) -> slip
    used_slip = set()     # id(slip) already claimed
    notes = []

    # --- Pass 1: the screenshot names the attachment it is showing --------------
    # Strongest signal available: a chat screenshot of a PDF displays that PDF's
    # file name. Matching is fuzzy, not exact - bank file names carry long digit
    # runs (RHB_17858159557471749_...) that get misread by a character or two, so
    # an exact comparison throws away a match that is obviously correct. The bar
    # stays high, because a wrong pairing puts one donor's name on another
    # donor's payment.
    def _best_slip(candidates, exact_only):
        best, best_score = None, 0
        for cand in candidates:
            c = _norm_name(cand)
            if not c:
                continue
            for s_ in slips:
                if id(s_) in used_slip:
                    continue
                target = _norm_name(s_.get("_name"))
                score = 100 if c == target else (0 if exact_only else fuzz.ratio(c, target))
                if score > best_score:
                    best, best_score = s_, score
        return best, best_score

    # Exact names first, so an unambiguous match claims its slip before a
    # fuzzier one can steal it.
    for exact_only in (True, False):
        for shot in shots:
            if id(shot) in paired_to:
                continue
            names = shot.get("attachment_filenames") or []
            if not names and shot.get("attachment_filename"):
                names = [shot["attachment_filename"]]
            if not names:
                continue
            slip, score = _best_slip(names, exact_only)
            if slip is not None and score >= (100 if exact_only else 88):
                paired_to[id(shot)] = slip
                used_slip.add(id(slip))
                how = "names this file" if exact_only else f"names this file ({score:.0f}% match)"
                notes.append(f"{shot.get('_name')} -> {slip.get('_name')} (screenshot {how})")
            elif not exact_only and slip is not None:
                notes.append(f"{shot.get('_name')}: closest file name only {score:.0f}% - left unpaired")

    # --- Build groups, preserving the original file order ----------------------
    shots_for = {}
    for shot in shots:
        slip = paired_to.get(id(shot))
        if slip is not None:
            shots_for.setdefault(id(slip), []).append(shot)

    groups = []
    for r in results:
        if r.get("source_kind") == "chat_screenshot" and id(r) in paired_to:
            continue                      # folded into its slip's group
        if r.get("source_kind") == "bank_slip":
            groups.append({"slip": r, "screenshots": shots_for.get(id(r), [])})
        else:
            # An unpaired screenshot (or unknown file) still becomes its own row
            # rather than being dropped - a volunteer must see that it exists.
            groups.append({"slip": None, "screenshots": [r]})

    for r in others:
        notes.append(f"{r.get('_name')}: not recognised as a slip or a chat screenshot - review it")

    return groups, notes


def merge_group(group: dict) -> dict:
    """
    Combine a slip and its screenshot(s) into one donation.

    Each source is trusted only for what it actually knows:
      - the bank slip for the transaction date and amount
      - the conversation for donor names, purpose, and a donor's own phone number
    A chat never shows the transaction date, so a screenshot must never supply it.
    """
    slip = group.get("slip")
    shots = group.get("screenshots") or []
    base = dict(slip) if slip else dict(shots[0])

    if slip and shots:
        # Prefer donor details from the conversation - the slip usually carries
        # only the payer's bank account name, not who the receipt is for.
        for shot in shots:
            donors = [d for d in (shot.get("donors") or []) if d.get("name")]
            if donors and not _looks_unknown(donors):
                base["donors"] = donors
                break
        for shot in shots:
            if shot.get("description"):
                base["description"] = shot["description"]
                break
        if not base.get("mobile"):
            for shot in shots:
                if shot.get("mobile"):
                    base["mobile"] = shot["mobile"]
                    break
        # Date and amount stay as the slip read them - never overwritten.
        base["date"] = slip.get("date", "")
        base["amount"] = slip.get("amount", 0)

    base["_source_file"] = ", ".join(
        [f.get("_name", "") for f in ([slip] if slip else []) + shots if f]
    )
    base["_drive_ids"] = [f.get("_drive_id") for f in ([slip] if slip else []) + shots
                          if f and f.get("_drive_id")]
    if not slip:
        base["_pairing_warning"] = "No bank slip paired - date/amount may be missing"
    return base


def _looks_unknown(donors: list) -> bool:
    return all(re.search(r"unknown|unidentified", str(d.get("name", "")), re.I) for d in donors)
