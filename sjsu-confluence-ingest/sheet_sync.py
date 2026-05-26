"""Google Sheet ingestion for front-desk lead Q&A overrides.

TODO(handoff): Senior dev setup notes
=====================================
This module uses Application Default Credentials (ADC). For local development,
ADC is the developer's gcloud login. For production (Cloud Run), it should be
a service account with the Sheets read scope.

Required setup steps before this works in production:

  1. Create or reuse a service account, e.g.:
     <service-account>@<your-gcp-project>.iam.gserviceaccount.com

  2. Share the target Google Sheet with that service account email as VIEWER
     (no Editor needed -- this code only reads).

  3. Mount the service account key into the Cloud Run Job as:
     GOOGLE_APPLICATION_CREDENTIALS=/secrets/sa-key.json
     OR use Cloud Run native identity (preferred).

  4. The service account does NOT need any extra OAuth scope configuration --
     service accounts can request the Sheets scope directly. This is the main
     reason this code may not run from a personal-account ADC if your Google
     Workspace admin restricts third-party OAuth scopes.

For local testing without API access, set SHEET_TEST_MODE=true to use a
hard-coded mock row instead of hitting the Sheets API.

READ-ONLY: This module only reads. There is no write path. The agent has no
mechanism to modify the Sheet -- only humans editing via the Google Sheets UI.

Sheet schema (Sheet1):
    A: Category       -- grouping/topic (optional)
    B: Question       -- what users ask
    C: Answer         -- authoritative answer
    D: Last Updated   -- date (free-text)
    E: Updated By     -- who edited (free-text)
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Iterator

import google.auth
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# READ-ONLY scope.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


@dataclass
class SheetEntry:
    """One row from the lead-maintained Q&A Sheet."""
    entry_id: str
    category: str
    question: str
    answer: str
    last_updated: str
    updated_by: str

    def to_rag_document(self) -> dict:
        """Render as a JSON doc for the RAG corpus.

        Structured so Gemini recognizes this as the authoritative override layer.
        """
        content_lines = [
            f"# {self.question}",
            "",
            "**Source:** SJSU IT Front Desk Q&A (lead-maintained, authoritative)",
        ]
        if self.category:
            content_lines.append(f"**Category:** {self.category}")
        if self.last_updated:
            line = f"**Last updated:** {self.last_updated}"
            if self.updated_by:
                line += f" by {self.updated_by}"
            content_lines.append(line)
        content_lines += ["", "## Answer", self.answer]

        return {
            "page_id": self.entry_id,
            "title": f"[Q&A] {self.question[:80]}",
            "content": "\n".join(content_lines),
            "url": "",
            "parent_id": None,
            "space_key": "SHEET",
            "last_updated": self.last_updated,
            "source": "sheet",
            "category": self.category,
            "is_authoritative": True,
        }


def _make_entry_id(question: str) -> str:
    """Stable hash so re-runs produce same IDs for same questions."""
    h = hashlib.sha1(question.lower().strip().encode()).hexdigest()[:16]
    return f"sheet-{h}"


def pull_sheet_entries(sheet_id: str, range_name: str = "Sheet1!A:E") -> Iterator[SheetEntry]:
    """Yield valid rows from the Sheet.

    Uses Application Default Credentials -- locally that is the developer's
    Google account; in Cloud Run it would be a service account.
    """
    # === Local test mode (SHEET_TEST_MODE=true) ===
    # TODO(handoff): Remove this block once production has real Sheets API access.
    # Exists so the pipeline can be tested end-to-end without depending on the
    # OAuth scope authorization that Google Workspace admin policy may block.
    import os as _os
    if _os.getenv("SHEET_TEST_MODE", "").lower() == "true":
        logger.info("SHEET_TEST_MODE=true -- yielding mock entry instead of calling API")
        yield SheetEntry(
            entry_id=_make_entry_id("Where is the front desk located?"),
            category="Location",
            question="Where is the front desk located?",
            answer="The IT front desk is at **Diaz Compean Student Union 1300**, "
                   "open Mon-Fri 8am-5pm.",
            last_updated="2026-05-22",
            updated_by="Saibhavana A. (mock)",
        )
        return

    try:
        creds, _ = google.auth.default(scopes=SCOPES)
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=range_name)
            .execute()
        )
    except Exception as e:
        logger.exception(f"Failed to read Sheet {sheet_id}: {e}")
        return

    rows = result.get("values", [])
    if not rows:
        logger.warning(f"Sheet {sheet_id} returned 0 rows")
        return

    # Detect header row by checking if first row has "question" anywhere
    header = rows[0] if rows else []
    has_header = header and any("question" in str(c).lower() for c in header)
    data_rows = rows[1:] if has_header else rows
    logger.info(
        f"Sheet: {len(data_rows)} data rows ({'with' if has_header else 'no'} header)"
    )

    valid = 0
    skipped = 0
    for row in data_rows:
        padded = list(row) + [""] * max(0, 5 - len(row))
        category = str(padded[0] or "").strip()
        question = str(padded[1] or "").strip()
        answer = str(padded[2] or "").strip()
        last_updated = str(padded[3] or "").strip()
        updated_by = str(padded[4] or "").strip()

        if not question or not answer:
            skipped += 1
            continue

        yield SheetEntry(
            entry_id=_make_entry_id(question),
            category=category,
            question=question,
            answer=answer,
            last_updated=last_updated,
            updated_by=updated_by,
        )
        valid += 1

    logger.info(f"Sheet: {valid} valid entries, {skipped} skipped (missing Q or A)")
