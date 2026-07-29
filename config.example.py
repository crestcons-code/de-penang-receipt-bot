# config.example.py
# Copy this file to config.py and fill in your credentials.

AUTOCOUNT = {
    "base_url": "https://accounting-api.autocountcloud.com",
    "key_id":   "your-key-id-here",
    "api_key":  "your-api-key-here",
    "company_id": "your-company-id-here",
}

DEFAULT_PAYMENT_METHOD = "BANK"
MAYBANK_GL_CODE = "310-1000"   # CASH AT BANK - MAYBANK
MATCH_THRESHOLD = 75

# Optional: Google Sheets integration for the volunteer dana-entry workflow.
# service_account_json_path: path to the downloaded service account JSON key file
#   (from Google Cloud Console > APIs & Services > Credentials).
# spreadsheet_id: the long ID in the dana list Google Sheet's URL, e.g.
#   https://docs.google.com/spreadsheets/d/THIS_PART_IS_THE_ID/edit
GOOGLE_SHEETS = {
    "service_account_json_path": "google_service_account.json",
    "spreadsheet_id": "your-spreadsheet-id-here",
}

# Optional: Anthropic API key for the AI slip-reading Dana Entry feature.
# Get one from https://console.anthropic.com (Settings > API Keys).
ANTHROPIC_API_KEY = "sk-ant-your-key-here"
