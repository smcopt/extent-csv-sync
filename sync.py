"""
sync.py — Daily sync of Site_Extents.csv from ZiteManager APIs
=================================================================
Pulls data from two ZiteManager report APIs:
  • API 5255: Site details (demographics, agency, location, etc.)
  • API 1856: Site extent polygons (WKT)

Merges them into the existing Site_Extents.csv on Google Drive.
Only ADDS new Site IDs and new/updated WKT — never overwrites existing rows.

Designed to run as a GitHub Actions cron job every 24 hours.
"""

import os
import io
import re
import json
import logging
import requests
import pandas as pd
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Configuration (from environment / GitHub Secrets) ────────────────────────
API_5255_URL = os.environ.get(
    "API_5255_URL",
    "https://app.zitemanager.org/api/v2/reports-file/?report_id=5255"
    "&key=tj1--akOPFgCbnbFx5dIBWCyVJk2632575916",
)
API_1856_URL = os.environ.get(
    "API_1856_URL",
    "https://app.zitemanager.org/api/v2/reports-file/?report_id=1856"
    "&key=JDw0cikwxjBMZ2oUlwJEZb8yAzY25818111819",
)

DRIVE_MASTER_FILE_ID = os.environ["DRIVE_MASTER_FILE_ID"]
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]

# Google OAuth creds
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]


# ─── Google Drive helpers ─────────────────────────────────────────────────────
def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("drive", "v3", credentials=creds)


def download_csv(service, file_id: str) -> pd.DataFrame:
    """Download the master CSV from Google Drive."""
    content = service.files().get_media(fileId=file_id).execute()
    df = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig")
    log.info("Downloaded master CSV: %d rows", len(df))
    return df


def upload_csv(service, file_id: str, df: pd.DataFrame):
    """Overwrite the master CSV on Google Drive."""
    buf = io.BytesIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    buf.seek(0)
    media = MediaIoBaseUpload(buf, mimetype="text/csv", resumable=True)
    service.files().update(fileId=file_id, media_body=media).execute()
    log.info("Uploaded updated master CSV: %d rows", len(df))


def create_backup(service, folder_id: str, df: pd.DataFrame):
    """Create/overwrite a rolling backup CSV in the same Drive folder."""
    buf = io.BytesIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    buf.seek(0)
    media = MediaIoBaseUpload(buf, mimetype="text/csv", resumable=True)

    query = f"name='Site_Extents_BACKUP.csv' and '{folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, spaces="drive", fields="files(id)").execute()
    files = results.get("files", [])

    if files:
        service.files().update(fileId=files[0]["id"], media_body=media).execute()
        log.info("Backup overwritten (existing file).")
    else:
        meta = {"name": "Site_Extents_BACKUP.csv", "parents": [folder_id]}
        service.files().create(body=meta, media_body=media, fields="id").execute()
        log.info("Backup created (new file).")


# ─── API fetch helpers ────────────────────────────────────────────────────────
def fetch_json(url: str, label: str) -> list[dict]:
    log.info("Fetching %s …", label)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    log.info("  → %d records", len(data))
    return data


# ─── Parsing / transformation logic ──────────────────────────────────────────
def parse_point(point_str: str) -> tuple[float | None, float | None]:
    """
    Parse 'POINT(lon lat)' → (longitude, latitude).
    The POINT format from ZiteManager is POINT(lon lat) with space separator.
    """
    if not point_str or not isinstance(point_str, str):
        return None, None
    m = re.match(r"POINT\(\s*([\d.\-]+)\s+([\d.\-]+)\s*\)", point_str.strip())
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def clean_agency(value: str | None) -> str:
    """Strip whitespace, remove lone dashes."""
    if not value or not isinstance(value, str):
        return ""
    v = value.strip()
    if v in ("-", "–", "—"):
        return ""
    return v


def compute_final_agency(managing: str, implementing: str) -> str:
    impl = clean_agency(implementing)
    mgmt = clean_agency(managing)
    return impl if impl else mgmt


def safe_int(val) -> str:
    """Convert to int string or empty string."""
    if val is None or val == "":
        return ""
    try:
        return str(int(float(val)))
    except (ValueError, TypeError):
        return ""


def compute_total_ind(row: dict) -> str:
    """
    If the total individuals field has a value, use it.
    Otherwise sum the 8 sex/age breakdown columns.
    """
    direct = row.get(
        "Site demographics/Estimated number of individuals currently accommodated in the site", ""
    )
    if direct and str(direct).strip():
        return safe_int(direct)

    age_cols = [
        "Site demographics/Number of MALES of age between 0 - 5 years",
        "Site demographics/Number of FEMALES of age between 0 - 5 years",
        "Site demographics/Number of MALES of age between 6 - 17 years",
        "Site demographics/Number of FEMALES of age between 6 - 17 years",
        "Site demographics/Number of MALES of age between 18 - 60 years",
        "Site demographics/Number of FEMALES of age between 18 - 60 years",
        "Site demographics/Number of MALES of age above 60 years",
        "Site demographics/Number of FEMALES of age above 60 years",
    ]
    total = 0
    any_value = False
    for col in age_cols:
        v = row.get(col, "")
        if v and str(v).strip():
            try:
                total += int(float(v))
                any_value = True
            except (ValueError, TypeError):
                pass
    return str(total) if any_value else ""


HH_COL = (
    "Site demographics/Please review the below information/"
    "Estimated number of households currently accommodated in the site "
    "(total population divided by 5 is ${est_hh_existing})"
)


def build_row_from_api(detail: dict, wkt_value: str = "") -> dict:
    """Transform one record from API 5255 (+ optional WKT from 1856) → CSV row."""
    lon, lat = parse_point(detail.get("Site Information/Location", ""))
    return {
        "Site_ID": detail.get("Site ID", ""),
        "Site_Name": detail.get("Site Name", ""),
        "Governorate": detail.get("Governorate", ""),
        "Governorate_PCODE": safe_int(detail.get("Region Information/First Level Region ID")),
        "Neighborhood": detail.get("Neighborhood", ""),
        "NeighborhoodPcode": safe_int(detail.get("Region Information/Second Level Region ID")),
        "Site_Agency": clean_agency(detail.get("Managing Agency")),
        "Implementing_Partner": clean_agency(detail.get("Implementing Partner")),
        "Site_Status": detail.get("Site Status", ""),
        "Site_Typology": detail.get("Displacement Type", ""),
        "Total_HH": safe_int(detail.get(HH_COL)),
        "Longitude": lon if lon is not None else "",
        "Latitude": lat if lat is not None else "",
        "Total_Ind": compute_total_ind(detail),
        "Final_Agency": compute_final_agency(
            detail.get("Managing Agency"), detail.get("Implementing Partner")
        ),
        "WKT": wkt_value,
    }


# ─── Main sync logic ─────────────────────────────────────────────────────────
def sync():
    # 1. Fetch both APIs
    details_data = fetch_json(API_5255_URL, "API 5255 (site details)")
    extent_data = fetch_json(API_1856_URL, "API 1856 (site extents)")

    # 2. Build lookup: Site ID → WKT from extent API
    wkt_lookup: dict[str, str] = {}
    for rec in extent_data:
        sid = rec.get("Site ID", "").strip()
        wkt_val = rec.get("Site Extent WKT [Most Recent Value]", "").strip()
        if sid and wkt_val:
            wkt_lookup[sid] = wkt_val
    log.info("WKT lookup built: %d sites with extents", len(wkt_lookup))

    # 3. Build lookup: Site ID → detail row from 5255 (Active sites only)
    detail_lookup: dict[str, dict] = {}
    skipped_inactive = 0
    for rec in details_data:
        sid = rec.get("Site ID", "").strip()
        status = rec.get("Site Status", "").strip()
        if sid and status == "Active":
            detail_lookup[sid] = rec
        elif sid:
            skipped_inactive += 1
    log.info("Detail lookup built: %d active sites (%d inactive skipped)", len(detail_lookup), skipped_inactive)

    # 4. Connect to Drive and download current master CSV
    service = get_drive_service()
    master_df = download_csv(service, DRIVE_MASTER_FILE_ID)

    # 5. Create backup BEFORE any changes
    create_backup(service, DRIVE_FOLDER_ID, master_df)

    # 6. Determine existing Site IDs in CSV
    existing_ids = set(master_df["Site_ID"].astype(str).str.strip())
    log.info("Existing sites in CSV: %d", len(existing_ids))

    # 7. Find NEW sites from API 5255 that are NOT in the CSV
    new_rows = []
    for sid, detail in detail_lookup.items():
        if sid not in existing_ids:
            wkt_val = wkt_lookup.get(sid, "")
            new_row = build_row_from_api(detail, wkt_val)
            new_rows.append(new_row)

    if new_rows:
        new_df = pd.DataFrame(new_rows, columns=master_df.columns)
        master_df = pd.concat([master_df, new_df], ignore_index=True)
        log.info("Added %d NEW site rows", len(new_rows))
    else:
        log.info("No new sites to add.")

    # 8. For EXISTING sites: only update WKT if the CSV cell is empty
    #    and the extent API now has a value
    wkt_updates = 0
    for idx, row in master_df.iterrows():
        sid = str(row["Site_ID"]).strip()
        current_wkt = str(row.get("WKT", "")).strip()

        # Only fill in WKT if the cell is currently empty/NaN
        if (not current_wkt or current_wkt.lower() == "nan") and sid in wkt_lookup:
            master_df.at[idx, "WKT"] = wkt_lookup[sid]
            wkt_updates += 1

    if wkt_updates:
        log.info("Filled WKT for %d existing sites (were empty, now have extent)", wkt_updates)
    else:
        log.info("No WKT backfills needed.")

    # 9. Upload updated CSV
    upload_csv(service, DRIVE_MASTER_FILE_ID, master_df)

    log.info(
        "Sync complete — %d new sites added, %d WKT backfills, %d total rows.",
        len(new_rows),
        wkt_updates,
        len(master_df),
    )


if __name__ == "__main__":
    sync()
