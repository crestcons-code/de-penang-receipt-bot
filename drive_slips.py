# drive_slips.py - read donation slips straight from a Google Drive folder.
#
# Lets volunteers bulk-share slips from WhatsApp into one Drive folder instead
# of downloading each to a PC and re-uploading it into the app.
#
# Read-only on purpose: the app never moves, renames or deletes anything in the
# volunteers' folder. Which slips have already been handled is tracked in the
# spreadsheet instead (see google_sheets_client.get_processed_slip_ids), so
# nothing here needs write access to Drive.
#
# Talks to the Drive REST API with plain requests rather than
# google-api-python-client: that package drags in its own protobuf and
# googleapis-common-protos, which can collide with the versions Streamlit
# already pins and take the whole app down at startup. google-auth and requests
# were already dependencies, and the three calls needed here are simple.

import requests
from google.auth.transport.requests import Request as _AuthRequest
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
API = "https://www.googleapis.com/drive/v3/files"

# Anything the slip reader can actually handle
SLIP_MIME_TYPES = ("image/jpeg", "image/png", "application/pdf")


class _Session:
    """Holds credentials and refreshes the access token when it expires."""

    def __init__(self, service_account_info: dict):
        self._creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)

    def _token(self) -> str:
        if not self._creds.valid:
            self._creds.refresh(_AuthRequest())
        return self._creds.token

    def get(self, url: str, **kwargs) -> requests.Response:
        headers = {"Authorization": f"Bearer {self._token()}"}
        r = requests.get(url, headers=headers, timeout=60, **kwargs)
        r.raise_for_status()
        return r


def get_service(service_account_info: dict) -> _Session:
    return _Session(service_account_info)


def find_folder(service: _Session, folder_name: str) -> str | None:
    """Return the folder's id, or None if it isn't shared with the service account."""
    safe = folder_name.replace("'", "\\'")
    r = service.get(API, params={
        "q": f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        "fields": "files(id, name)",
        "pageSize": 10,
    })
    files = r.json().get("files", [])
    return files[0]["id"] if files else None


def list_slips(service: _Session, folder_id: str) -> list:
    """
    List the slip files in a folder, oldest first so the batch order matches the
    order they were sent. Files Drive has converted to its own formats (e.g. a
    CSV opened as a Google Sheet) are skipped - they can't be read as bytes.
    """
    out = []
    page = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken, files(id, name, mimeType, size, modifiedTime)",
            "orderBy": "modifiedTime",
            "pageSize": 200,
        }
        if page:
            params["pageToken"] = page
        data = service.get(API, params=params).json()
        for f in data.get("files", []):
            if f.get("mimeType") in SLIP_MIME_TYPES:
                out.append(f)
        page = data.get("nextPageToken")
        if not page:
            break
    return out


def download(service: _Session, file_id: str) -> bytes:
    return service.get(f"{API}/{file_id}", params={"alt": "media"}).content
