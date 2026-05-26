"""
SJSU IT Confluence → JSON ingestion script.

Calls the Application Integration trigger, receives Confluence pages,
cleans HTML to markdown, writes one JSON file per page to ./output/.
"""

import json
import logging
import os
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request
from markdownify import markdownify as html_to_md
from tenacity import retry, stop_after_attempt, wait_exponential
from google.cloud import storage

# === RAG corpus refresh ===
# TODO(handoff): Senior dev — confirm RAG_CORPUS_ID in .env matches the
# corpus the agent reads from. This module deletes + reimports files on
# every run; that is idempotent but does ~30-60s of work.
import vertexai
from vertexai import rag
from sheet_sync import pull_sheet_entries

# ============ Configuration ============
load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID", "sjsu-it-genai-poc")
LOCATION = os.getenv("LOCATION", "us-west1")
INTEGRATION_NAME = os.getenv("INTEGRATION_NAME", "SG_Confluence_Test")
TRIGGER_ID = os.getenv("TRIGGER_ID", "api_trigger/SG_Confluence_Test_API_1")
DOMAIN_URL = os.getenv("DOMAIN_URL", "https://sjsu-its.atlassian.net")
SPACE_KEY = os.getenv("SPACE_KEY", "SDKB")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./output"))
GCS_BUCKET = os.getenv("GCS_BUCKET", "")
GCS_PREFIX = os.getenv("GCS_PREFIX", "confluence")

INTEGRATION_URL = (
    f"https://{LOCATION}-integrations.googleapis.com/v1/"
    f"projects/{PROJECT_ID}/locations/{LOCATION}/"
    f"integrations/{INTEGRATION_NAME}:execute"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ============ Authentication ============
def get_access_token() -> str:
    credentials, _ = google_auth_default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(Request())
    return credentials.token


# ============ Fetch pages ============
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def fetch_pages_from_integration() -> list[dict]:
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "triggerId": TRIGGER_ID,
        "inputParameters": {
            "filterClause": {
                "stringValue": f"space_key = '{SPACE_KEY}'"
            }
        },
    }

    logger.info(f"POST {INTEGRATION_URL}")
    logger.info(f"Body: {json.dumps(body)}")
    response = requests.post(INTEGRATION_URL, headers=headers, json=body, timeout=180)

    if not response.ok:
        logger.error(f"HTTP {response.status_code}: {response.text[:2000]}")
        response.raise_for_status()

    data = response.json()
    output_params = data.get("outputParameters", {})

    # Try multiple known locations / names for the page list
    candidate_keys = [
        "connectorOutputPayload",
        "`Task_1_connectorOutputPayload`",
        "Task_1_connectorOutputPayload",
    ]
    payload = None
    for key in candidate_keys:
        if key in output_params:
            payload = output_params[key]
            logger.info(f"Found payload under key: {key}")
            break

    if payload is None:
        logger.error(f"No payload found. Available keys: {list(output_params.keys())}")
        logger.error(f"Full response (first 2k): {json.dumps(data, indent=2)[:2000]}")
        return []

    # Output parameters are usually typed: {"jsonValue": [...]}, {"stringValue": "..."}
    if isinstance(payload, dict):
        for typed in ("jsonValue", "stringValue", "jsonObjectValue"):
            if typed in payload:
                payload = payload[typed]
                break

    # If still a string, parse it
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            logger.error(f"Payload is unparseable string: {payload[:500]}")
            return []

    # Unwrap dict-with-list shapes
    if isinstance(payload, dict):
        for key in ("data", "results", "records", "pages", "items"):
            if key in payload and isinstance(payload[key], list):
                payload = payload[key]
                break
        else:
            payload = [payload]  # single record

    if not isinstance(payload, list):
        logger.error(f"Unexpected payload type after unwrap: {type(payload)}")
        return []

    logger.info(f"Received {len(payload)} pages")
    return payload


# ============ HTML cleaning ============
def clean_confluence_html(html: str) -> str:
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    drop_macros = {
        "children", "toc", "pagetree", "include", "detailssummary",
        "create-from-template", "sharelinks-urlmacro", "anchor",
        "excerpt-include", "metadata-summary",
    }
    keep_body_macros = {"info", "note", "warning", "tip", "panel", "expand", "excerpt"}

    for macro in soup.find_all("ac:structured-macro"):
        name = macro.get("ac:name", "")
        if name in drop_macros:
            macro.decompose()
        elif name in keep_body_macros:
            body = macro.find("ac:rich-text-body") or macro.find("ac:plain-text-body")
            if body:
                macro.replace_with(body)
            else:
                macro.decompose()

    for tag in soup.find_all(["ac:layout", "ac:layout-section", "ac:layout-cell"]):
        tag.unwrap()

    for user in soup.find_all("ri:user"):
        user.replace_with("@user")

    for link in soup.find_all("ac:link"):
        page_ref = link.find("ri:page")
        if page_ref:
            title = page_ref.get("ri:content-title", "linked page")
            link.replace_with(f"[{title}]")

    for img in soup.find_all("ac:image"):
        img.decompose()

    markdown = html_to_md(str(soup), heading_style="ATX", strip=["script", "style"])
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return markdown


# ============ URL fix ============
def fix_url(url: str) -> str:
    if not url:
        return ""
    return url.replace("${DOMAIN_URL}", DOMAIN_URL)


# ============ Transform a page ============
def transform_page(page: dict) -> dict | None:
    """Convert a raw page record into the clean ingestion JSON shape."""
    # The connector returns PascalCase fields
    page_id = page.get("Id") or page.get("id") or page.get("content_id")
    title = page.get("Title") or page.get("title", "")
    raw_html = page.get("Storage") or page.get("description") or page.get("body", "")
    url = page.get("URL") or page.get("LinksWebui") or page.get("url", "")
    last_modified = page.get("LastUpdatedDatetime") or page.get("lastModified") or page.get("CreatedDate", "")
    entity_type = page.get("Type") or page.get("entity_type", "page")
    excerpt = page.get("Excerpt", "")
    space_key_resp = page.get("SpaceKey") or SPACE_KEY
    parent_id = page.get("ParentId")
    parent_id = str(parent_id) if parent_id else ""

    if not page_id:
        logger.warning(f"Skipping page with no ID. Keys: {list(page.keys())[:10]}")
        return None

    # Fix relative URLs - LinksWebui returns "/wiki/spaces/..." so we prepend domain
    if url.startswith("/"):
        url = f"{DOMAIN_URL}{url}"
    # Connector's URL field is missing /wiki/ - add it
    if url and "/wiki/" not in url:
        url = url.replace(f"{DOMAIN_URL}/spaces/", f"{DOMAIN_URL}/wiki/spaces/")
    # Construct URL if still missing
    if not url:
        url = f"{DOMAIN_URL}/wiki/spaces/{space_key_resp}/pages/{page_id}"

    cleaned = clean_confluence_html(raw_html)
    if len(cleaned) < 50:
        logger.info(f"Skipping {page_id} '{title}' \u2014 {len(cleaned)} chars after cleaning")
        return None

    return {
        "page_id": str(page_id),
        "title": title,
        "url": url,
        "content": cleaned,
        "excerpt": excerpt,
        "parent_id": parent_id,
        "last_modified": last_modified,
        "entity_type": entity_type,
        "space_key": space_key_resp,
    }



# ============ Main ============

def upload_to_gcs(records: list[dict]) -> None:
    """Upload each record as a JSON file to GCS, and delete stale files."""
    if not GCS_BUCKET:
        logger.warning("GCS_BUCKET not set, skipping cloud upload")
        return

    client = storage.Client(project=PROJECT_ID)
    bucket = client.bucket(GCS_BUCKET)

    current_ids = set()
    uploaded = 0
    for rec in records:
        page_id = rec["page_id"]
        current_ids.add(page_id)
        blob_path = f"{GCS_PREFIX}/{page_id}.json"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(
            json.dumps(rec, indent=2, ensure_ascii=False),
            content_type="application/json",
        )
        uploaded += 1

    logger.info(f"Uploaded {uploaded} files to gs://{GCS_BUCKET}/{GCS_PREFIX}/")

    # Delete bucket files for pages no longer in Confluence
    deleted = 0
    for blob in bucket.list_blobs(prefix=f"{GCS_PREFIX}/"):
        if not blob.name.endswith(".json"):
            continue
        page_id_from_name = blob.name.replace(f"{GCS_PREFIX}/", "").replace(".json", "")
        if page_id_from_name not in current_ids:
            blob.delete()
            deleted += 1
            logger.info(f"Deleted stale: {blob.name}")
    if deleted:
        logger.info(f"Removed {deleted} stale files from bucket")


def refresh_rag_corpus() -> None:
    """Delete + reimport all files in the Vertex AI RAG corpus.

    Uses the delete+reimport pattern so deletions (Confluence pages removed
    from SDKB, Sheet rows deactivated) propagate cleanly.

    Vertex AI rate-limits delete to ~60/min per region. We add a small
    sleep between calls and retry once on quota errors to stay under it.
    """
    import time
    corpus_id = os.getenv("RAG_CORPUS_ID", "").strip()
    project_id = os.getenv("PROJECT_ID", "sjsu-it-genai-poc")
    location = os.getenv("LOCATION", "us-west1")

    if not corpus_id:
        logger.warning("RAG_CORPUS_ID not set in .env -- skipping corpus refresh")
        return

    vertexai.init(project=project_id, location=location)
    corpus_name = f"projects/{project_id}/locations/{location}/ragCorpora/{corpus_id}"

    logger.info(f"Listing existing files in RAG corpus {corpus_id}")
    existing = list(rag.list_files(corpus_name=corpus_name))
    logger.info(f"Found {len(existing)} existing files -- deleting (rate-limited)")

    deleted = 0
    for i, f in enumerate(existing, 1):
        for attempt in range(3):
            try:
                rag.delete_file(name=f.name)
                deleted += 1
                break
            except Exception as e:
                msg = str(e).lower()
                if "quota" in msg or "rate" in msg or "limit" in msg:
                    wait = 2 ** attempt
                    logger.info(f"Rate limited on {f.name}, retrying in {wait}s")
                    time.sleep(wait)
                else:
                    logger.warning(f"Could not delete {f.name}: {e}")
                    break
        # Pace ourselves: ~50/min target = 1.2s between calls
        time.sleep(1.2)
        if i % 20 == 0:
            logger.info(f"  ...deleted {i}/{len(existing)}")

    logger.info(f"Deleted {deleted} of {len(existing)} files")

    gcs_uri = f"gs://{GCS_BUCKET}/{GCS_PREFIX}/"
    logger.info(f"Reimporting from {gcs_uri}")
    rag.import_files(
        corpus_name=corpus_name,
        paths=[gcs_uri],
        transformation_config=rag.TransformationConfig(
            chunking_config=rag.ChunkingConfig(
                chunk_size=512,
                chunk_overlap=100,
            ),
        ),
    )
    logger.info("RAG corpus refresh complete")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Output dir: {OUTPUT_DIR.absolute()}")
    logger.info(f"Integration: {INTEGRATION_NAME}, Trigger: {TRIGGER_ID}")
    logger.info(f"Space filter: {SPACE_KEY}")

    pages = fetch_pages_from_integration()
    if not pages:
        logger.error("No pages received. Exiting.")
        return

    written = 0
    skipped = 0
    records_for_gcs = []
    for page in pages:
        record = transform_page(page)
        if record is None:
            skipped += 1
            continue
        out_path = OUTPUT_DIR / f"{record['page_id']}.json"
        out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        written += 1
        records_for_gcs.append(record)

    logger.info(f"Done locally. {written} written, {skipped} skipped.")

    # === Pull Q&A overrides from Google Sheet ===
    sheet_id = os.getenv("SHEET_ID", "").strip()
    sheet_range = os.getenv("SHEET_RANGE", "Sheet1!A:E")
    sheet_count = 0
    if sheet_id:
        logger.info(f"Pulling Sheet overrides: {sheet_id}")
        for entry in pull_sheet_entries(sheet_id, sheet_range):
            record = entry.to_rag_document()
            out_path = OUTPUT_DIR / f"{record['page_id']}.json"
            out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
            records_for_gcs.append(record)
            sheet_count += 1
        logger.info(f"Sheet: {sheet_count} entries appended to corpus")
    else:
        logger.info("SHEET_ID not set, skipping Sheet ingestion")

    upload_to_gcs(records_for_gcs)
    refresh_rag_corpus()


if __name__ == "__main__":
    main()
