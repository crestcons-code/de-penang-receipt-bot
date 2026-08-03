#!/usr/bin/env python
"""
Emergency password / role recovery for the DE Penang receipt app.

Use this when nobody can get into the app's Admin tab - e.g. the only admin
forgot their password, or an admin was accidentally removed or demoted.

It edits users.yaml in the PRIVATE user-list repo directly, changing only the
one account you name and leaving everyone else untouched. Passwords are hashed
locally with bcrypt before being sent; the plain password is never printed,
logged, or written to disk.

Usage:
    python reset_password.py                 # interactive
    python reset_password.py --list          # just show the current users
    python reset_password.py --dry-run       # show what would change, write nothing

Needs a GitHub token that can write to the private repo. It looks for one in
this order:
    1. GITHUB_TOKEN environment variable
    2. the GitHub CLI  (`gh auth token`)  - normally already set up on this PC
    3. GITHUB_TOKEN in config.py
"""

import argparse
import base64
import getpass
import os
import subprocess
import sys

import bcrypt
import requests
import yaml

USERS_REPO = os.environ.get("USERS_REPO", "crestcons-code/de-penang-users")
API_URL = f"https://api.github.com/repos/{USERS_REPO}/contents/users.yaml"


def get_token() -> str:
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
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from config import GITHUB_TOKEN  # type: ignore
        if GITHUB_TOKEN:
            return GITHUB_TOKEN
    except Exception:
        pass

    sys.exit("No GitHub token found. Set GITHUB_TOKEN, or run 'gh auth login' first.")


def headers(token: str) -> dict:
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}


def fetch_users(token: str):
    """Return (users_dict, file_sha). Exits with a clear message on failure."""
    r = requests.get(API_URL, headers=headers(token), timeout=15)
    if r.status_code == 404:
        sys.exit(f"Could not find users.yaml in {USERS_REPO}.\n"
                 "Check the repo name and that your token can read it.")
    if r.status_code == 401:
        sys.exit("GitHub rejected the token (401). It may have expired.")
    r.raise_for_status()
    payload = r.json()
    data = yaml.safe_load(base64.b64decode(payload["content"]))
    if not data or "usernames" not in data:
        sys.exit("users.yaml is empty or malformed - refusing to touch it.")
    return data, payload["sha"]


def save_users(token: str, data: dict, sha: str, message: str) -> None:
    content = yaml.dump(data, default_flow_style=False, allow_unicode=True)
    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "sha": sha,
    }
    r = requests.put(API_URL, headers=headers(token), json=body, timeout=15)
    if r.status_code not in (200, 201):
        sys.exit(f"Failed to save ({r.status_code}): {r.text[:300]}")


def show_users(users: dict) -> None:
    print(f"\nUsers in {USERS_REPO}:")
    for name, info in sorted(users.items()):
        print(f"  {name:<15} {info.get('name', ''):<22} role={info.get('role', 'user')}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Recover an app password or admin role.")
    ap.add_argument("--list", action="store_true", help="list users and exit")
    ap.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
    ap.add_argument("--username", help="account to change (skips the prompt)")
    args = ap.parse_args()

    token = get_token()
    data, sha = fetch_users(token)
    users = data["usernames"]

    show_users(users)
    if args.list:
        return

    username = args.username or input("Username to recover: ").strip().lower()
    if username not in users:
        sys.exit(f"No such user: {username!r}. Run with --list to see valid names.")

    current_role = users[username].get("role", "user")

    # --- new password (optional: blank keeps the existing one) ---
    pw1 = getpass.getpass("New password (leave blank to keep current): ")
    new_hash = None
    if pw1:
        if len(pw1) < 8:
            sys.exit("Password must be at least 8 characters.")
        pw2 = getpass.getpass("Confirm new password: ")
        if pw1 != pw2:
            sys.exit("Passwords do not match. Nothing changed.")
        new_hash = bcrypt.hashpw(pw1.encode(), bcrypt.gensalt()).decode()

    # --- optional role restore ---
    make_admin = False
    if current_role != "admin":
        ans = input(f"'{username}' is currently role={current_role}. Make admin? [y/N]: ").strip().lower()
        make_admin = ans == "y"

    if not new_hash and not make_admin:
        print("Nothing to change.")
        return

    # Change ONLY this account; everyone else is left exactly as-is
    before_count = len(users)
    if new_hash:
        users[username]["password"] = new_hash
    if make_admin:
        users[username]["role"] = "admin"

    # Sanity check before writing - never shrink or mangle the list
    assert len(data["usernames"]) == before_count, "user count changed - aborting"
    assert all("password" in v for v in users.values()), "a user lost their password - aborting"

    changes = []
    if new_hash:
        changes.append("password")
    if make_admin:
        changes.append("role -> admin")
    summary = f"{username}: {', '.join(changes)}"

    if args.dry_run:
        print(f"\n[dry run] Would change {summary}")
        print(f"[dry run] {before_count} users would remain. Nothing was written.")
        return

    save_users(token, data, sha, f"Recovery: update {summary}")
    print(f"\nDone - {summary}.")
    print("You can log in with the new password now (existing sessions stay valid).")


if __name__ == "__main__":
    main()
