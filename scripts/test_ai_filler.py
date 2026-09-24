"""Test AI form filler on a Figma Greenhouse application."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path.home() / "job-agent-integrations" / ".env")
# Also load job-agent's env (for GLM_API_KEY)
load_dotenv(Path.home() / "job-agent" / ".env")

from integrations.ai_form_filler import ai_fill_form


PROFILE = {
    "first_name": "Ahmet",
    "last_name": "Kaya",
    "email": "aaahmetkayaaa@gmail.com",
    "phone": "+31 6 15034058",
    "country": "Netherlands",
    "location": "Almelo, Netherlands",
    "work_authorization": "Eligible to work in the Netherlands",
    "linkedin": "",
    "summary": (
        "Business Operations professional with 4 years of experience "
        "in supplier coordination, order tracking, and process automation. "
        "Currently building a personal Python automation project (job-agent) "
        "that integrates multiple APIs and LLM workflows."
    ),
    "skills": ["Python", "REST APIs", "Excel", "Streamlit", "SQLite"],
    "languages": "English (fluent), Turkish (native), Dutch (A1/A2)",
}


JOB_DESCRIPTION = """
Distribution Partner Manager at Figma.
We are looking for a partner manager to grow our distribution network.
Responsibilities: manage partner relationships, drive adoption, coordinate
cross-functional projects. Requirements: 3+ years in partnerships or
operations, strong communication, project management skills.
"""


def main():
    result = ai_fill_form(
        job_url="https://boards.greenhouse.io/figma/jobs/5813967004",
        profile=PROFILE,
        job_description=JOB_DESCRIPTION,
        cv_path="/Users/ahmet/job-agent/static/cv/172_cv.pdf",
        cl_path=None,
        headless=False,
        keep_open_seconds=120,
    )

    print()
    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    print(f"Success: {result.get('success')}")
    print(f"Message: {result.get('message')}")
    if result.get("decisions"):
        print()
        print(f"LLM Decisions ({len(result['decisions'])}):")
        for d in result["decisions"]:
            print(f"  [{d.get('action')}] {d.get('id')} = {str(d.get('value'))[:60]}")
    if result.get("apply_result"):
        print()
        print("Apply stats:", result["apply_result"]["stats"])
        print()
        print("Details:")
        for line in result["apply_result"]["details"]:
            print(f"  {line}")


if __name__ == "__main__":
    main()
