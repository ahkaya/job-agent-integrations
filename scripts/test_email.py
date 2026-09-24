"""Test the email_sender module with a dry run."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from integrations.email_sender import extract_email_from_text, send_application_email


# Test 1: Email extraction from job description
job_description = """
Wij zoeken een Business Operations Specialist.
Stuur je CV naar solliciteren@voorbeeldbedrijf.nl
Of bel 06-12345678 voor meer informatie.
"""
extracted = extract_email_from_text(job_description)
print(f"[TEST 1] Extracted email: {extracted}")
assert extracted == "solliciteren@voorbeeldbedrijf.nl", "Email extraction failed"
print("[TEST 1] PASSED")

# Test 2: Dry run email send
cv_path = Path("test_cv.pdf")
cv_path.write_bytes(b"%PDF-1.4 fake pdf for testing\n")

result = send_application_email(
    to_email="test@example.com",
    position="Business Operations Specialist",
    company="Voorbeeld BV",
    cover_letter="Dear Hiring Manager,\n\nThis is a test cover letter.\n\nBest,\nAhmet",
    cv_pdf_path=str(cv_path),
    dry_run=True,
)
print(f"[TEST 2] Result: {result}")
assert result["success"], f"Dry run failed: {result['message']}"
print("[TEST 2] PASSED")

# Cleanup
cv_path.unlink()

print()
print("[ALL TESTS PASSED]")
