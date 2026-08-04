#!/usr/bin/env python
"""
Backup the DE Penang dana list Google Sheet.

Saves every tab into one timestamped .xlsx file, kept in two places so a single
mishap can't lose both:

  1. a local folder on this PC          (fast to restore from)
  2. a PRIVATE GitHub repo              (off-site - survives theft, fire, ransomware)

The most likely way this data is lost is not a hacker, it's an ordinary
accident: a volunteer sorting or deleting rows, or a bad paste over the sheet.
Google Sheets' own version history helps, but only for ~30 days and only if
someone notices in time - these snapshots are the longer-term safety net.

Usage:
    python backup_sheet.py                 # local + off-site (normal weekly run)
    python backup_sheet.py --local-only    # skip the GitHub upload
    python backup_sheet.py --keep 20       # keep 20 local snapshots (default 12)
"""

import argparse
import base64
import datetime as _dt
import json
import os
import subprocess
import sys

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BACKUP_REPO = os.environ.get("BACKUP_REPO", "crestcons-code/de-penang-backups")
DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")


def get_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    try:
        from config import GITHUB_TOKEN  # type: ignore
        return GITHUB_TOKEN
    except Exception:
        return ""


def fetch_all_tabs():
    """Return (dict of tab_name -> DataFrame, spreadsheet_title)."""
    from config_loader import get_google_sheets_config
    from google_sheets_client import get_client

    cfg = get_google_sheets_config()
    info, sid = cfg.get("service_account_info"), cfg.get("spreadsheet_id")
    if not info or not sid:
        sys.exit("Google Sheets isn't configured - check GOOGLE_SHEETS in config.py.")

    client = get_client(info)
    sh = client.open_by_key(sid)

    tabs = {}
    for ws in sh.worksheets():
        values = ws.get_all_values()
        # Keep everything as raw text, header row included - this is an archive,
        # not something to re-parse. Dates and amounts must survive exactly as
        # the volunteers typed them, so no type inference and no header handling.
        tabs[ws.title] = pd.DataFrame(values) if values else pd.DataFrame()
    return tabs, sh.title


def write_xlsx(tabs: dict, path: str) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, df in tabs.items():
            # Excel sheet names: max 31 chars, and none of : \ / ? * [ ]
            safe = name[:31]
            for ch in ':\\/?*[]':
                safe = safe.replace(ch, "-")
            df.to_excel(writer, sheet_name=safe or "Sheet", index=False, header=False)


def upload_to_github(path: str, token: str) -> bool:
    with open(path, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    name = os.path.basename(path)
    url = f"https://api.github.com/repos/{BACKUP_REPO}/contents/{name}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    # If a file with this name already exists (same-day re-run), replace it
    sha = ""
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            sha = r.json().get("sha", "")
    except Exception:
        pass

    body = {"message": f"Backup {name}", "content": content}
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=headers, json=body, timeout=60)
    if r.status_code not in (200, 201):
        print(f"  ! Off-site upload failed ({r.status_code}): {r.text[:200]}")
        return False
    return True


def prune_local(folder: str, keep: int) -> None:
    files = sorted(f for f in os.listdir(folder) if f.startswith("dana-backup-") and f.endswith(".xlsx"))
    for old in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(os.path.join(folder, old))
            print(f"  removed old local backup: {old}")
        except Exception as e:
            print(f"  could not remove {old}: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backup the dana list Google Sheet.")
    ap.add_argument("--local-only", action="store_true", help="skip the off-site GitHub copy")
    ap.add_argument("--keep", type=int, default=12, help="how many local snapshots to keep (default 12)")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="local backup folder")
    args = ap.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(args.dir, f"dana-backup-{stamp}.xlsx")

    print("Reading the Google Sheet...")
    tabs, title = fetch_all_tabs()
    rows = sum(len(df) for df in tabs.values())
    print(f"  '{title}': {len(tabs)} tab(s), {rows} rows total")

    if rows == 0:
        sys.exit("Refusing to save an empty backup - the sheet returned no rows. "
                 "Check the connection and try again.")

    write_xlsx(tabs, path)
    size_kb = os.path.getsize(path) / 1024
    print(f"Saved local backup: {path}  ({size_kb:.0f} KB)")

    if not args.local_only:
        token = get_github_token()
        if not token:
            print("  ! No GitHub token found - local backup only.")
        elif upload_to_github(path, token):
            print(f"Uploaded off-site copy to private repo {BACKUP_REPO}")

    prune_local(args.dir, args.keep)
    print("\nBackup complete.")


if __name__ == "__main__":
    main()
