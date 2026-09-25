"""Gmail → Sheets → job-agent tracker.

Reads new rows from a Google Sheet (populated by Make.com from Gmail),
classifies each email with the LLM (interview / rejection / offer / info),
writes the result to job-agent's database, and marks the row as processed.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

from openai import OpenAI

# Make sure job-agent modules are importable
import sys
JOB_AGENT_ROOT = Path(__file__).resolve().parent.parent.parent / "job-agent"
if JOB_AGENT_ROOT.exists():
    sys.path.insert(0, str(JOB_AGENT_ROOT))


CLASSIFY_PROMPT = """You are an email classifier for job applications.

You receive an email (subject, sender, snippet) that may be a response to a
job application. Classify it into ONE of these categories:

- "interview"  — invitation to interview / call / meeting
- "rejection"  — decline / not moving forward / unfortunately
- "offer"      — job offer / contract proposal
- "info"       — acknowledgment, request for more info, recruiter screen, other
- "irrelevant" — not related to a job application

Return ONLY valid JSON:
{
  "category": "interview" | "rejection" | "offer" | "info" | "irrelevant",
  "confidence": 0.0-1.0,
  "reason": "one short sentence"
}
"""


def _load_sheets():
    """Lazy import gspread (only needed when tracking)."""
    import gspread
    from google.oauth2.service_account import Credentials

    sheet_id = os.environ.get("GOOGLE_SHEETS_ID")
    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "config/gcp_service_account.json")

    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEETS_ID is not set")
    if not Path(creds_path).exists():
        raise RuntimeError(f"Service account JSON not found: {creds_path}")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(sheet_id)
    return sheet.sheet1


def classify_email(subject, sender, snippet=""):
    """Call GLM to classify the email. Returns a dict."""
    api_key = os.environ.get("GLM_API_KEY")
    if not api_key:
        raise RuntimeError("GLM_API_KEY is not set")

    client = OpenAI(api_key=api_key, base_url="https://api.z.ai/api/paas/v4/")

    user_msg = f"""Email:
- From: {sender}
- Subject: {subject}
- Snippet: {snippet[:800]}
"""

    response = client.chat.completions.create(
        model="glm-5.3-flash",
        messages=[
            {"role": "system", "content": CLASSIFY_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=500,
    )
    content = response.choices[0].message.content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        try:
            retry = client.chat.completions.create(
                model="glm-5.3-flash",
                messages=[
                    {"role": "system", "content": CLASSIFY_PROMPT},
                    {"role": "user", "content": user_msg + "\n\nReturn ONLY a single-line JSON object. No newlines inside strings."},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=500,
            )
            return json.loads(retry.choices[0].message.content)
        except Exception:
            return {"category": "info", "confidence": 0.0, "reason": "LLM parse error (retried)"}


def _match_job_id(sender, subject, conn):
    """Try to find the related job_id in the jobs table.

    Strategy:
      1. Extract company name from sender email domain
      2. Search jobs.company for a match
      3. Fallback: return None
    """
    sender = (sender or "").lower()
    subject = subject or ""

    # Extract domain from sender (e.g. hr@spotify.com → spotify)
    domain = ""
    m = re.search(r"@([a-zA-Z0-9.-]+)", sender)
    if m:
        domain = m.group(1).split(".")[0].lower()

    # Search jobs by company name matching the domain
    if domain and domain not in ("gmail", "outlook", "hotmail", "yahoo", "protonmail"):
        rows = conn.execute("""
            SELECT job_id, company FROM jobs
            WHERE LOWER(company) LIKE ? AND is_applied = 1
            ORDER BY applied_at DESC
            LIMIT 1
        """, (f"%{domain}%",)).fetchall()
        if rows:
            return rows[0][0], rows[0][1]

    # Fallback: search by any word in the subject (>= 4 chars)
    for word in re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", subject):
        rows = conn.execute("""
            SELECT job_id, company FROM jobs
            WHERE LOWER(company) LIKE ? AND is_applied = 1
            ORDER BY applied_at DESC
            LIMIT 1
        """, (f"%{word.lower()}%",)).fetchall()
        if rows:
            return rows[0][0], rows[0][1]

    return None, None


def sync_sheet_to_db(verbose=True):
    """Read new rows from the sheet, classify, write to DB, mark processed.

    Returns: dict with counts.
    """
    import sys
    sys.path.insert(0, str(JOB_AGENT_ROOT))
    from data.database import get_connection

    ws = _load_sheets()
    rows = ws.get_all_records()  # list of dicts

    stats = {"total_rows": len(rows), "new": 0, "classified": 0,
             "matched": 0, "unmatched": 0, "irrelevant": 0, "errors": 0}

    # Column order: email_id, sender, subject, received_at, processed
    # gspread get_all_records() uses the header row automatically.

    conn = get_connection()
    try:
        for idx, row in enumerate(rows, start=2):  # data starts at row 2
            email_id = (row.get("email_id") or "").strip()
            sender = (row.get("sender") or "").strip()
            subject = (row.get("subject") or "").strip()
            snippet = (row.get("snippet") or "").strip()
            received_at = (row.get("received_at") or "").strip()
            processed = str(row.get("processed") or "").strip().lower()

            if not email_id:
                continue
            if processed in ("true", "1", "yes"):
                continue

            stats["new"] += 1

            # Skip if already in DB
            existing = conn.execute(
                "SELECT email_id FROM application_emails WHERE email_id = ?",
                (email_id,),
            ).fetchone()
            if existing:
                # Mark as processed anyway
                ws.update_cell(idx, 6, "TRUE")
                continue

            # Classify with LLM
            try:
                classification = classify_email(subject, sender, snippet)
            except Exception as e:
                if verbose:
                    print(f"  [ERR] classify {email_id}: {e}")
                stats["errors"] += 1
                continue

            category = classification.get("category", "info")
            confidence = float(classification.get("confidence", 0.0))
            reason = classification.get("reason", "")
            stats["classified"] += 1

            if category == "irrelevant":
                stats["irrelevant"] += 1
                ws.update_cell(idx, 6, "TRUE")
                continue

            # Try to match to a job
            job_id, company = _match_job_id(sender, subject, conn)

            if job_id:
                stats["matched"] += 1
            else:
                stats["unmatched"] += 1

            # Insert into application_emails
            conn.execute("""
                INSERT OR REPLACE INTO application_emails (
                    email_id, job_id, sender, subject, snippet,
                    received_at, category, confidence, reason, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (
                email_id, job_id, sender, subject, "",
                received_at, category, confidence, reason,
            ))

            # Update jobs.application_status
            if job_id:
                conn.execute("""
                    UPDATE jobs
                    SET application_status = ?,
                        application_status_updated_at = datetime('now')
                    WHERE job_id = ?
                """, (category, job_id))

            conn.commit()

            # Mark the sheet row as processed
            ws.update_cell(idx, 6, "TRUE")

            if verbose:
                print(f"  [{category}] {sender[:30]} | {subject[:50]} | "
                      f"job_id={job_id or 'unmatched'}")

    finally:
        conn.close()

    return stats


def get_new_responses_count():
    """Return the count of unread (unseen) application emails."""
    import sys
    sys.path.insert(0, str(JOB_AGENT_ROOT))
    from data.database import get_connection

    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT COUNT(*) FROM application_emails
            WHERE processed_at IS NOT NULL
        """).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path.home() / "job-agent" / ".env")
    load_dotenv(Path.home() / "job-agent-integrations" / ".env")

    print("Syncing sheet → DB...")
    stats = sync_sheet_to_db(verbose=True)
    print()
    print("Stats:", stats)
