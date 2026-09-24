# job-agent-integrations

External automation integrations for [job-agent](https://github.com/ahkaya/job-agent).

This repository demonstrates how the core `job-agent` system can be extended
with third-party automation tools:

1. **Send applications by email** (SMTP)
2. **Autofill ATS application forms** (Selenium + LLM)
3. **(coming)** Track application status via Gmail → Make.com webhook

## Why a separate repo?

The core `job-agent` stays lean. Integration code that talks to external
services lives here so the core remains deployable without Selenium,
Gmail creds, etc.

## Modules

### `integrations/email_sender.py` — SMTP email applications

For postings that say "email your CV to solliciteren@company.nl".

- Extracts the application email from a job description
- Sends the cover letter as the body
- Attaches the CV PDF
- Uses a Gmail App Password

### `integrations/ai_form_filler_universal.py` — AI form filling

For ATS forms with an "Apply" button (Greenhouse, Lever, ...).

Pipeline:
1. Selenium opens the application page.
2. Extract all form fields (id, name, type, label, required, options).
3. LLM returns one decision per field (`fill`, `autocomplete`, `select`,
   `upload`, `click`, `skip`).
4. Selenium applies the decisions.
5. **Never submits** — the user reviews and clicks Submit.

#### ATS autofill coordination

Some ATS platforms (e.g. Lever) parse the uploaded CV and autofill name /
email / phone / location themselves. This filler:
- Uploads the CV first
- Waits 8 seconds for the ATS parser
- Skips fields that are already populated
- Overwrites only fields that are still empty or semantically wrong
  (e.g. "Current company" → "Not currently employed")

#### Autocomplete handling

For autocomplete inputs (country, location):
- Types the value
- Waits for the dropdown
- Scores every option (`exact` > `starts_with` > `city_name` > `word_match`)
- Clicks the best match via JavaScript (avoids stale-element errors)

Tested with:
- **Greenhouse** (`job-boards.greenhouse.io/figma`)
- **Lever** (`jobs.lever.co/spotify`)

### `integrations/form_filler.py` — Greenhouse-only (legacy)

Older, simpler version. Kept for reference.

## Setup

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env

Fill in:
- `GMAIL_ADDRESS` — your Gmail address
- `GMAIL_APP_PASSWORD` — Gmail App Password (from myaccount.google.com/apppasswords)
- `GLM_API_KEY` — from https://z.ai/

## Usage

Test the email sender:

    python3 scripts/test_email.py

Autofill a Greenhouse / Lever form:

    python3 scripts/test_universal_lever.py

The script opens Chrome, fills the form, and leaves it open so you can
review and click Submit yourself.

## Safety

- **Never auto-submits.** The user clicks Submit.
- **Stale-element safe.** Re-finds elements, uses JS click fallbacks.
- **ATS-autofill aware.** Leaves fields the ATS already filled alone.
- **No credentials in git.** `.env` is gitignored.

## License

MIT
