# job-agent-integrations

External automation integrations for [job-agent](https://github.com/ahkaya/job-agent).

This repository demonstrates how the core `job-agent` system can be extended
with third-party automation and AI tools:

1. **Send applications by email** (SMTP)
2. **Autofill ATS application forms** (Selenium + LLM, never auto-submits)
3. **Track application responses** end-to-end:
   Gmail -> Make.com -> Google Sheets -> Ansible on GitHub Actions -> LLM -> Turso -> Telegram

## Why a separate repo?

The core `job-agent` stays lean. Integration code that talks to external
services lives here so the core remains deployable without Selenium,
Gmail creds, cloud DB, etc.

---

## Modules

### `integrations/email_sender.py` - SMTP email applications

For postings that say "email your CV to solliciteren@company.nl".

- Extracts the application email from a job description
- Sends the cover letter as the body
- Attaches the CV PDF
- Uses a Gmail App Password

### `integrations/ai_form_filler_universal.py` - AI form filling

For ATS forms with an "Apply" button (Greenhouse, Lever, Ashby).

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

#### Professional CV filenames

Before uploading, the CV is copied to a temp file with a professional name
built from the job metadata (`2026-09-25_<Company>_<Position>_CV.pdf`)
instead of the internal job ID. Temp files are cleaned up by the OS.

#### Dashboard integration

The job-agent Streamlit dashboard exposes this filler as a
**"Apply with Autofill"** button on the *Generated CVs* page. The button:

- Runs Selenium in a separate process so the dashboard UI stays responsive
- Passes the CV PDF matching the selected job
- Only enables for Greenhouse, Lever, and Ashby URLs
- Never submits - the user reviews the form and clicks Submit manually

Tested with:

- **Greenhouse** (`job-boards.greenhouse.io/figma`)
- **Lever** (`jobs.lever.co/spotify`)

### `integrations/gmail_tracker.py` - Gmail -> Sheets -> LLM -> Turso

Classifies application-response emails and writes the result back to the
`job-agent` database, closing the application loop.

Pipeline:

1. **Make.com** watches Gmail for new messages matching application-related
   keywords (`interview`, `application`, `sollicitatie`, `vacature`,
   `afwijzing`, `uitnodiging`, `update`, `status`, `decision`, ...).
2. Make.com writes each match to a Google Sheet (`Job Applications Tracker`)
   with columns:
   `email_id | sender | subject | snippet | received_at | processed`.
3. **GitHub Actions** runs an **Ansible playbook** on a cron schedule
   (Mon-Fri, 08:30-16:30 CEST, every 30 minutes).
4. `gmail_tracker.py` reads new rows (`processed=FALSE`), sends
   **subject + sender + snippet** to the LLM (GLM).
5. The LLM returns a category: `interview` | `rejection` | `offer` |
   `info` | `irrelevant`, plus a confidence score and a one-sentence reason.
6. Rows are written to `application_emails` in the **Turso cloud database**
   (libSQL), matched to a `job_id` by sender domain
   (e.g. `hr@spotify.com` -> `spotify`).
7. **Telegram notifications** are sent for actionable responses
   (`interview`, `offer`). Rejections and info are stored silently.
8. The Sheet row is marked `processed=TRUE`.

**Robustness:** The classifier uses `response_format=json_object`, a
single retry with `temperature=0` on JSON parse failure, and a graceful
`info` fallback. Failed rows stay `processed=FALSE` so the next run retries
them - no email is lost.

Run it manually:

    python3 integrations/gmail_tracker.py

### `integrations/form_filler.py` - Greenhouse-only (legacy)

Older, simpler version. Kept for reference.

---

## Cloud architecture

The response-tracking pipeline runs entirely on free tiers:

| Component | Role | Free tier |
|---|---|---|
| **Make.com** | Gmail watcher -> Sheets | 1,000 ops/month |
| **GitHub Actions** | Cron runner (Ansible) | 2,000 min/month (private repos) |
| **Ansible** | Environment setup + task runner | Open source |
| **Turso (libSQL)** | Cloud SQLite database | 5 GB storage, 500M row reads |
| **GLM (Z.AI)** | LLM classifier | Pay-per-use |
| **Telegram Bot API** | Push notifications | Free |

### Data flow

    Gmail
      -> Make.com (filter + write to Sheets)
        -> Google Sheets
          -> GitHub Actions (cron, every 30 min)
            -> Ansible playbook
              -> gmail_tracker.py (Python + GLM)
                -> Turso cloud DB  (written)
                -> Telegram        (notified)
                -> Sheets          (marked processed)

The **job-agent** desktop dashboard pulls from the same Turso DB via
`pyturso`, so local and cloud always see the same data.

### Turso setup

1. `turso db create job-agent`
2. `turso db show job-agent --url` -> `TURSO_SYNC_URL`
3. `turso db tokens create job-agent` -> `TURSO_TOKEN`
4. Put both in `.env` (local) and in GitHub Actions secrets (CI)

`data/database.py` in `job-agent` uses `pyturso` with
`remote_url` + `auth_token`, so the same SQLite file is synced to Turso
automatically via `conn.pull()` / `conn.push()`.

### GitHub Actions secrets

| Secret | Purpose |
|---|---|
| `GLM_API_KEY` | LLM classifier |
| `TELEGRAM_BOT_TOKEN` | Telegram notifications |
| `TELEGRAM_CHAT_ID` | Telegram target chat |
| `GOOGLE_SHEETS_ID` | Sheet to read |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Sheets access (single-line JSON) |
| `GMAIL_ADDRESS` | Sender address |
| `TURSO_SYNC_URL` | Cloud DB URL (`https://...turso.io`) |
| `TURSO_TOKEN` | Cloud DB auth token |

---

## Setup

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env

Fill in:

- `GMAIL_ADDRESS` - your Gmail address
- `GMAIL_APP_PASSWORD` - Gmail App Password
- `GLM_API_KEY` - from https://z.ai/
- `GOOGLE_SHEETS_ID` - the `Job Applications Tracker` spreadsheet ID
- `GOOGLE_SERVICE_ACCOUNT_JSON` - path to the GCP service account JSON
  (default: `config/gcp_service_account.json`)
- `TURSO_SYNC_URL` - Turso cloud DB URL
- `TURSO_TOKEN` - Turso auth token

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

Run the CI playbook locally:

    ansible-playbook ansible/playbook.yml

---

## Safety

- **Never auto-submits.** The user clicks Submit on every form.
- **Stale-element safe.** Re-finds elements, uses JS click fallbacks.
- **ATS-autofill aware.** Leaves fields the ATS already filled alone.
- **No credentials in git.** `.env`, `config/`, and DB files are gitignored.
- **No lost emails.** LLM errors leave `processed=FALSE` for retry.
- **Never invents data.** The LLM only classifies; it does not write
  free-form text to the DB.

## License

MIT
