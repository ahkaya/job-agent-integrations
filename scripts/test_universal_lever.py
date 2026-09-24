import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path.home() / "job-agent-integrations" / ".env")
load_dotenv(Path.home() / "job-agent" / ".env")

from integrations.ai_form_filler_universal import ai_fill_form


PROFILE = {
    "first_name": "Ahmet",
    "last_name": "Kaya",
    "email": "aaahmetkayaaa@gmail.com",
    "phone": "+31 6 15034058",
    "location": "Almelo, Netherlands",
    "country": "Netherlands",
    "linkedin": "",
    "summary": (
        "Business Operations professional with 4 years of experience "
        "in supplier coordination, order tracking, and process automation."
    ),
}

JOB_DESCRIPTION = """
Client Partner - Emerging & Scaled, Independent Agency (UK) at Spotify.
Requires 5+ years in media/advertising, strong stakeholder management.
"""


def main():
    result = ai_fill_form(
        job_url="https://jobs.lever.co/spotify/35e07377-d8a5-47b4-b3f1-c443de3c86dd/apply",
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
