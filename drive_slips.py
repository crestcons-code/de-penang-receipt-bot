# drive_slips.py - read donation slips straight from a Google Drive folder.
#
# Lets volunteers bulk-share slips from WhatsApp into one Drive folder instead
# of downloading each to a PC and re-uploading it into the app.
#
# Read-only on purpose: the app never moves, renames or deletes anything in the
# volunteers' folder. Which slips have already been handled is tracked in the
# spreadsheet instead (see google_sheets_client.processed_slip_ids), so nothing
# here needs write access to Drive.

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Anything the slip reader can actually handle
SLIP_MIME_TYPES = ("image/jpeg", "image/png", "application/pdf")


def get_service(service_account_info: dict):
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_folder(service, folder_name: str) -> str | None:
    """Return the folder's id, or None if it isn't shared with the service account."""
    safe = folder_name.replace("'", "\\'")
    res = service.files().list(
        q=f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        fields="files(id, name)", pageSize=10,
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def list_slips(service, folder_id: str) -> list:
    """
    List the slip files in a folder, oldest first so the batch order matches the
    order they were sent. Files Drive has converted to its own formats (e.g. a
    CSV opened as a Google Sheet) are skipped - they can't be read as bytes.
    """
    out = []
    page = None
    while True:
        res = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
            orderBy="modifiedTime", pageSize=200, pageToken=page,
        ).execute()
        for f in res.get("files", []):
            if f.get("mimeType") in SLIP_MIME_TYPES:
                out.append(f)
        page = res.get("nextPageToken")
        if not page:
            break
    return out


def download(service, file_id: str) -> bytes:
    return service.files().get_media(fileId=file_id).execute()
