# donation_mapping.py
# Maps keywords found in bank transaction descriptions to Autocount GL account codes.
# Keywords are case-insensitive. First match wins — put more specific terms first.
# Edit this file to add/change mappings without touching any other code.

DONATION_MAP = [
    # GL Code    Short description          Keywords to match
    ("500-9005", "Tree House",              ["tree house", "treehouse", "veranda"]),
    ("500-5001", "Lamp Lighting",           ["lamp light", "pelita"]),
    ("500-5002", "Cheng Beng",              ["cheng beng", "qing ming"]),
    ("500-5003", "Wesak",                   ["wesak", "waisak", "kuti", "sima"]),
    ("500-5004", "Kathina",                 ["kathina"]),
    ("500-5005", "Sala Hall",               ["sala hall", "sala"]),
    ("500-5006", "Farm to Dana",            ["farm to dana", "farm dana"]),
    ("500-5007", "CNY",                     ["cny", "chinese new year", "tahun baru cina"]),
    ("500-5008", "Vassa",                   ["vassa"]),
    ("500-5009", "No.2 Jalan Sungai Ara 5", ["jalan sungai ara 5", "sungai ara 5"]),
    ("500-5010", "Q-Sun",                   ["q-sun", "qsun"]),
    ("500-5011", "No.1 Jalan Sungai Ara 7", ["jalan sungai ara 7", "sungai ara 7"]),
    ("500-5012", "Lot 77 & 318 Land",       ["lot 77", "lot 318", "land"]),
    ("500-5013", "Lot 77 & 318 Building",   ["building"]),
    ("500-6000", "Monk & Nun Requisites",   ["monk", "nun", "requisite", "robe"]),
    ("500-7000", "SP Meditation Point",     ["sp meditation", "meditation point"]),
    ("500-8000", "TCM",                     ["tcm", "medical"]),
    ("500-9000", "Paritta Group",           ["paritta"]),
    ("500-9001", "Parami Group",            ["parami"]),
    ("500-9002", "Mahadana",                ["mahadana"]),
    ("500-9003", "Mangala Family",          ["mangala"]),
    ("500-9004", "Dhamma Propagation",      ["dhamma propagation", "ecosystem"]),
    ("500-4000", "General Donation",        []),   # fallback — always last
]

FALLBACK_GL = "500-4000"   # DONATION - GENERAL

# Maps GL code to Autocount Department No. (blank = no department)
GL_DEPARTMENT = {
    "500-5004": "KATHINA",
    "500-5010": "Q-SUN",
    "500-7000": "SP",
    "500-8000": "TCM",
    "500-9000": "PARITTA",
    "500-9001": "PARAMI",
    "500-9002": "MAHADANA",
    "500-9003": "MANGALA",
}


def map_to_gl(description: str) -> tuple[str, str, str]:
    """
    Match description to a donation GL account.
    Returns (gl_code, gl_account_description, short_description).
    """
    desc_lower = description.lower()
    for gl_code, short_desc, keywords in DONATION_MAP:
        for kw in keywords:
            if kw in desc_lower:
                return gl_code, f"DONATION - {short_desc.upper()}", short_desc
    return FALLBACK_GL, "DONATION - GENERAL", "General Donation"


def get_department(gl_code: str) -> str:
    """Return the department code for a GL, or empty string if none."""
    return GL_DEPARTMENT.get(gl_code, "")


def _gl_name(code: str) -> str:
    for gl_code, short_desc, _ in DONATION_MAP:
        if gl_code == code:
            return short_desc
    return code
