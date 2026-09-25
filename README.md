# job-agent-integrations

External automation integrations for [job-agent](https://github.com/ahkaya/job-agent).

This repository demonstrates how the core `job-agent` system can be extended
with third-party automation tools:

1. **Send applications by email** (SMTP)
2. **Autofill ATS application forms** (Selenium + LLM)
3. **Track application status** via Gmail -> Make.com -> Sheets -> LLM -> DB

## Why a separate repo?

The core `job-agent` stays lean. Integration code that talks to external
services lives here so the core remains deployable without Selenium,
Gmail creds, etc.

---

## Modules

### `integrations/email_sender.py` - SMTP email applications

For postings that say "email your CV to solliciteren@company.nl".

- Extracts the application email from a job description
- Sends the cover letter as the body
- Attaches the CV PDF
- Uses a Gmail App Password

### `integrations/ai_form_filler_universal.py` - AI form filling

For ATS forms with an "Apply" button (Greenhouse, Lever, ...).

Pipeline:

1. Selenium opens the application page.
2. Extract all form fields (id, name, type, label, required, options).
3. LLM returns one decision per field (`fill`, `autocomplete`, `select`,
   `upload`, `click`, `skip`).
4. Selenium applies the decisions.
5. **Never submits** - the user reviews and clicks Submit.

#### ATS autofill coordination

Some ATS platforms (e.g. Lever) parse the uploaded CV and autofill name /
email / phone / location themselves. This filler:

- Uploads the CV first
- Waits 8 seconds for the ATS parser
- Skips fields that are already populated
- Overwrites only fields that are still empty or semantically wrong
  (e.g. "Current company" -> "Not currently employed")

#### Autocomplete handling

For autocomplete inputs (country, location):

- Types the value
- Waits for the dropdown
- Scores every option (`exact` > `starts_with` > `city_name` > `word_match`)
- Clicks the best match via JavaScript (avoids stale-element errors)

Tested with:

- **Greenhouse** (`job-boards.greenhouse.io/figma`)
- **Lever** (`jobs.lever.co/spotify`)

### `integrations/gmail_tracker.py` - Gmail -> Sheets -> LLM -> DB

Classifies application-response emails and writes the result back to
`job-agent`'s database, closing the application loop.

Pipeline:

1. **Make.com** watches Gmail for new messages matching application-related
   keywords (`interview`, `application`, `sollicitatie`, `vacature`,
   `afwijzing`, `uitnodiging`, `update`, `status`, `decision`, ...).
2. Make.com writes each match to a Google Sheet (`Job Applications Tracker`)
   with columns:
   `email_id | sender | subject | snippet | received_at | processed`.
3. `gmail_tracker.py` reads new rows (`processed=FALSE`), sends
   **subject + sender + snippet** to the LLM (GLM).
4. The LLM returns a category: `interview` | `rejection` | `offer` | `info` |
   `irrelevant`, plus a confidence score and a one-sentence reason.
5. Rows are written to `application_emails` in `job-agent`'s SQLite DB.
   `job_id` is matched by sender domain (e.g. `hr@spotify.com` -> `spotify`)
   against the `jobs` table.
6. The Sheet row is marked `processed=TRUE`.

**Robustness:** The classifier uses `response_format=json_object`, a
single retry with `temperature=0` on JSON parse failure, and a graceful
`info` fallback. Failed rows stay `processed=FALSE` so the next run retries
them - no email is lost.

Run it manually:

    python3 integrations/gmail_tracker.py

Or schedule it via cron / launchd for continuous sync.

### `integrations/form_filler.py` - Greenhouse-only (legacy)

Older, simpler version. Kept for reference.

---

## Make.com scenario (Gmail -> Sheets)

The Make.com scenario is the front half of the tracking pipeline. It is a
visual, no-code automation that can be inspected and edited without
touching code.

**Modules:**

1. **Gmail - Watch Emails** (polling, every 15 min on the free plan)
   - Filter type: `Gmail filter`
   - Query:
     `subject:(interview OR application OR sollicitatie OR vacature OR afwijzing OR uitnodiging OR update OR status OR decision OR candidate OR confirmation OR thank OR applying)`
2. **Google Sheets - Add a Row**
   - Spreadsheet: `Job Applications Tracker`
   - Column mapping:
     - `email_id`    <- Gmail -> Message ID
     - `sender`      <- Gmail -> From Email
     - `subject`     <- Gmail -> Subject
     - `snippet`     <- Gmail -> Snippet
     - `received_at` <- Gmail -> Date (formatDate(...; "YYYY-MM-DDTHH:mm:ss[Z]"))
     - `processed`   <- `FALSE` (literal)

**Auth:** Google Sheets OAuth via a dedicated GCP OAuth Client (project
`job-agent-tracker-509622`, external, test mode). The `aaahmetkayaaa@gmail.com`
account is added as a test user.

**Why Make.com here:** the JD for the target role explicitly mentions
Zapier / Integromat (Make.com) / Ansible / Selenium. This scenario is a
working Make.com automation that feeds a Python + LLM backend - i.e. the
"no-code -> code" bridge the role asks for.

---

## Setup

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env

Fill in:

- `GMAIL_ADDRESS` - your Gmail address
- `GMAIL_APP_PASSWORD` - Gmail App Password (from myaccount.google.com/apppasswords)
- `GLM_API_KEY` - from https://z.ai/
- `GOOGLE_SHEETS_ID` - the `Job Applications Tracker` spreadsheet ID
- `GOOGLE_SERVICE_ACCOUNT_JSON` - path to the GCP service account JSON
  (default: `config/gcp_service_account.json`)
- `JOB_AGENT_DB_PATH` - path to `job-agent/data/jobs.db`
  (default: `../job-agent/data/jobs.db`)

The service account must be shared on the spreadsheet as **Editor**.

---

## Usage

Test the email sender:

    python3 scripts/test_email.py

Autofill a Greenhouse / Lever form:

    python3 scripts/test_universal_lever.py

The script opens Chrome, fills the form, and leaves it open so you can
review and click Submit yourself.

Sync new responses from the Sheet into the DB:

    python3 integrations/gmail_tracker.py

---

## Safety

- **Never auto-submits.** The user clicks Submit on every form.
- **Stale-element safe.** Re-finds elements, uses JS click fallbacks.
- **ATS-autofill aware.** Leaves fields the ATS already filled alone.
- **No credentials in git.** `.env` and `config/` are gitignored.
- **No lost emails.** LLM errors leave `processed=FALSE` for retry.
- **Never invents data.** The LLM only classifies; it does not write
  free-form text to the DB.

## License

MIT
