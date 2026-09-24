"""SMTP email sender for job applications.

Sends a job application email with:
- Custom subject line (position + applicant name)
- Cover letter as email body
- CV (PDF) and/or Cover Letter (PDF) as attachments
"""

import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path


def extract_email_from_text(text):
    """Extract the first email address from a job description.

    Returns None if no email is found.
    """
    if not text:
        return None
    pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    matches = re.findall(pattern, text)
    if not matches:
        return None

    # Filter out common non-application emails
    exclude = ("noreply", "no-reply", "donotreply", "example.com", "test.com")
    for m in matches:
        if not any(x in m.lower() for x in exclude):
            return m
    return None


def send_application_email(
    to_email,
    position,
    company,
    cover_letter,
    cv_pdf_path,
    cl_pdf_path=None,
    sender_name="Ahmet Kaya",
    sender_email=None,
    sender_password=None,
    dry_run=False,
):
    """Send a job application email.

    Args:
        to_email: recipient email
        position: job title
        company: company name
        cover_letter: email body (plain text)
        cv_pdf_path: path to CV PDF
        cl_pdf_path: optional path to cover letter PDF
        sender_name: applicant name
        sender_email: Gmail address (falls back to GMAIL_ADDRESS env)
        sender_password: Gmail app password (falls back to GMAIL_APP_PASSWORD env)
        dry_run: if True, only print what would happen

    Returns:
        dict with keys: success (bool), message (str)
    """
    sender_email = sender_email or os.environ.get("GMAIL_ADDRESS")
    sender_password = sender_password or os.environ.get("GMAIL_APP_PASSWORD")

    if not sender_email or not sender_password:
        return {"success": False, "message": "GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set"}

    if not to_email:
        return {"success": False, "message": "Recipient email is empty"}

    # Build message
    msg = EmailMessage()
    msg["Subject"] = f"Application: {position} - {sender_name}"
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = to_email
    msg.set_content(cover_letter or f"Dear Hiring Manager,\n\nPlease find my application for the {position} position at {company}.\n\nBest regards,\n{sender_name}")

    # Attach CV
    cv_path = Path(cv_pdf_path)
    if cv_path.exists():
        msg.add_attachment(
            cv_path.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=cv_path.name,
        )
    else:
        return {"success": False, "message": f"CV PDF not found: {cv_pdf_path}"}

    # Attach cover letter PDF if provided
    if cl_pdf_path:
        cl_path = Path(cl_pdf_path)
        if cl_path.exists():
            msg.add_attachment(
                cl_path.read_bytes(),
                maintype="application",
                subtype="pdf",
                filename=cl_path.name,
            )

    # Dry run — don't actually send
    if dry_run:
        return {
            "success": True,
            "message": f"[DRY RUN] Would send to {to_email} | Subject: {msg['Subject']} | Attachments: {cv_path.name}" + (f", {Path(cl_pdf_path).name}" if cl_pdf_path else ""),
        }

    # Send via Gmail SMTP
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(sender_email, sender_password)
            smtp.send_message(msg)
        return {"success": True, "message": f"Email sent to {to_email}"}
    except smtplib.SMTPAuthenticationError:
        return {"success": False, "message": "Gmail authentication failed. Check App Password."}
    except smtplib.SMTPException as e:
        return {"success": False, "message": f"SMTP error: {e}"}
    except Exception as e:
        return {"success": False, "message": f"Unexpected error: {e}"}
