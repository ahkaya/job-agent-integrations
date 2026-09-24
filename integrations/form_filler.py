"""Greenhouse form filler using Selenium.

Opens a Greenhouse job application page, fills in the standard fields
from a candidate profile, uploads the CV/cover letter PDFs, and STOPS
before submitting so the user can review.

Tested against boards.greenhouse.io (classic version).
"""

import os
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


def build_driver(headless=False):
    """Create a Chrome driver with anti-bot-friendly options."""
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--window-size=1280,900")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def _try_fill(driver, selector, value, timeout=5):
    """Fill an input if it exists. Returns True on success."""
    if value is None or value == "":
        return False
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )
        el.clear()
        el.send_keys(str(value))
        return True
    except Exception:
        return False


def _try_upload(driver, selector, file_path, timeout=5):
    """Upload a file to an <input type=file>. Returns True on success."""
    if not file_path or not Path(file_path).exists():
        return False
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )
        el.send_keys(str(Path(file_path).resolve()))
        return True
    except Exception:
        return False


def _fill_standard_fields(driver, profile):
    """Fill name, email, phone, country, location."""
    filled = {}
    filled["first_name"] = _try_fill(driver, "#first_name", profile.get("first_name"))
    filled["last_name"] = _try_fill(driver, "#last_name", profile.get("last_name"))
    filled["email"] = _try_fill(driver, "#email", profile.get("email"))

    # Country: this is a text input on Figma-style forms
    # Send the full number with country code
    phone = profile.get("phone", "")
    filled["phone"] = _try_fill(driver, "#phone", phone)
    filled["country"] = _try_fill(driver, "#country", profile.get("country_code", "Netherlands"))

    if profile.get("location"):
        filled["location"] = _try_fill(driver, "#candidate-location", profile.get("location"))

    return filled


def _fill_required_questions(driver, answers):
    """Fill custom question fields (textarea or text) by field id.

    answers: dict mapping element id -> answer text
    """
    filled = {}
    for field_id, answer in answers.items():
        if not answer:
            continue
        selector = f"#{field_id}"
        filled[field_id] = _try_fill(driver, selector, answer, timeout=3)
    return filled


def _upload_cover_letter(driver, file_path):
    """Click the Attach button (if needed) and upload the cover letter.

    On Figma-style forms, the cover letter goes into a textarea
    (#question_15508266004) or via Attach. We try the textarea route.
    """
    if not file_path or not Path(file_path).exists():
        return False
    # Try direct file input first (some forms have separate hidden input)
    try:
        inputs = driver.find_elements(By.CSS_SELECTOR, "input[type=file]")
        for inp in inputs:
            iid = (inp.get_attribute("id") or "").lower()
            if "cover" in iid or "additional" in iid:
                inp.send_keys(str(Path(file_path).resolve()))
                return True
    except Exception:
        pass
    return False


def fill_greenhouse_application(
    job_url,
    profile,
    cv_pdf_path,
    cl_pdf_path=None,
    question_answers=None,
    headless=False,
    keep_open_seconds=600,
):
    """Open a Greenhouse application form and fill standard fields.

    Args:
        job_url: Greenhouse job posting URL
        profile: dict with first_name, last_name, email, phone, country_code, location
        cv_pdf_path: path to CV PDF
        cl_pdf_path: optional cover letter PDF path
        question_answers: dict mapping element id -> answer (for custom questions)
        headless: run Chrome invisibly
        keep_open_seconds: how long to leave browser open

    Returns:
        dict with keys: success, message, filled
    """
    if not job_url or "greenhouse.io" not in job_url:
        return {"success": False, "message": "Not a Greenhouse URL"}

    driver = build_driver(headless=headless)
    try:
        driver.get(job_url)
        time.sleep(4)

        filled = _fill_standard_fields(driver, profile)

        # CV upload
        cv_uploaded = _try_upload(driver, "#resume", cv_pdf_path)

        # Cover letter (optional — try textarea route if file fails)
        cl_uploaded = False
        if cl_pdf_path:
            cl_uploaded = _upload_cover_letter(driver, cl_pdf_path)

        # Custom questions
        q_filled = {}
        if question_answers:
            q_filled = _fill_required_questions(driver, question_answers)

        # Keep browser open for manual review + submit
        time.sleep(keep_open_seconds)

        return {
            "success": True,
            "message": (
                f"Standard fields: {sum(1 for v in filled.values() if v)}/6, "
                f"CV: {cv_uploaded}, Cover: {cl_uploaded}, "
                f"Questions: {sum(1 for v in q_filled.values() if v)}/{len(question_answers or {})}. "
                "Review and click Apply manually."
            ),
            "filled": filled,
            "questions_filled": q_filled,
        }
    except Exception as e:
        import traceback
        return {"success": False, "message": f"Error: {e}", "trace": traceback.format_exc()}
    finally:
        driver.quit()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    profile = {
        "first_name": "Ahmet",
        "last_name": "Kaya",
        "email": "aaahmetkayaaa@gmail.com",
        "phone": "+31 6 15034058",
        "country_code": "Netherlands",
        "location": "Almelo, Netherlands",
    }

    answers = {
        "question_15508264004": (
            "I want to join Figma because the company's mission of making design "
            "accessible to all resonates with my own experience building digital "
            "products from the ground up. As a co-founder, I learned how much "
            "collaboration and tooling matter for a small team to move fast, and "
            "Figma's platform is exactly the kind of product I admire. I'd love "
            "to bring my operations and automation skills to a company that "
            "believes great things are never made alone."
        ),
        "question_15508267004": "Almelo, Netherlands",
        "question_15508270004": "No",
    }

    result = fill_greenhouse_application(
        job_url="https://boards.greenhouse.io/figma/jobs/5813967004",
        profile=profile,
        cv_pdf_path="/Users/ahmet/job-agent/static/cv/172_cv.pdf",
        question_answers=answers,
        headless=False,
        keep_open_seconds=60,
    )
    print(result)
