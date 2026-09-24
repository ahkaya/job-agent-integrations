"""AI-powered form filler.

Selenium extracts the form structure (all inputs, selects, textareas,
file inputs, with their labels and requirements), sends it to the LLM
together with the candidate profile, and applies the LLM's decisions.

The form is NEVER submitted automatically — the browser stays open
for the user to review and click Submit manually.
"""

import json
import os
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from webdriver_manager.chrome import ChromeDriverManager


# ============================================================
# 1. DRIVER
# ============================================================

def build_driver(headless=False):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--window-size=1280,1000")
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


# ============================================================
# 2. FORM EXTRACTION
# ============================================================

EXTRACT_JS = r"""
const elements = document.querySelectorAll('input, textarea, select');
const results = [];

elements.forEach((el, idx) => {
    const id = el.id || el.name || ('auto_' + idx);
    const tag = el.tagName.toLowerCase();
    const type = tag === 'select' ? 'select' : (el.type || tag);

    // Label detection (multiple strategies)
    let label = '';
    if (el.id) {
        const lbl = document.querySelector(`label[for='${el.id}']`);
        if (lbl) label = (lbl.textContent || '').trim();
    }
    if (!label) {
        // aria-label
        label = el.getAttribute('aria-label') || '';
    }
    if (!label) {
        // Parent label
        const parentLabel = el.closest('label');
        if (parentLabel) label = (parentLabel.textContent || '').trim();
    }
    if (!label) {
        // Sibling label with id ending in -label
        const sibling = el.parentElement?.querySelector('label');
        if (sibling) label = (sibling.textContent || '').trim();
    }

    // Required
    const required = el.required || el.getAttribute('aria-required') === 'true';

    // Options (for select)
    let options = null;
    if (tag === 'select') {
        options = Array.from(el.options).map(o => ({
            value: o.value,
            text: (o.textContent || '').trim(),
        }));
    }

    // Skip reCAPTCHA, hidden, and irrelevant fields
    if (type === 'hidden') return;
    if (id.toLowerCase().includes('recaptcha')) return;
    if (id.toLowerCase().startsWith('g-recaptcha')) return;

    // Skip demographic / voluntary fields (they are optional)
    const idLower = id.toLowerCase();
    const skipIds = ['gender', 'hispanic_ethnicity', 'veteran_status',
                     'disability_status', 'race', 'ethnicity'];
    const isDemographic = skipIds.some(s => idLower.includes(s));

    results.push({
        id: id,
        tag: tag,
        type: type,
        name: el.name || '',
        label: label.substring(0, 200),
        required: required,
        placeholder: el.placeholder || '',
        options: options,
        is_demographic: isDemographic,
        visible: el.offsetParent !== null,
    });
});

return results;
"""


def extract_form_fields(driver):
    """Extract all form fields with their metadata."""
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.5)

    fields = driver.execute_script(EXTRACT_JS)

    # Python-side autocomplete detection (reliable fallback)
    AUTOCOMPLETE_KEYWORDS = (
        "country", "location", "city", "state", "region",
        "address", "postal", "zip",
    )
    # Labels that imply a Yes/No dropdown (custom, not native <select>)
    YESNO_LABEL_PATTERNS = (
        "have you ever", "are you authorized", "do you have",
        "do you require", "are you willing", "are you legally",
        "have you previously", "did you", "will you",
    )
    for f in fields:
        fid = (f.get("id") or "").lower()
        label = (f.get("label") or "").lower()
        ftype = (f.get("type") or "").lower()

        # Only text-like inputs can be autocomplete
        if ftype not in ("text", "tel", ""):
            f["is_autocomplete"] = False
            continue

        # Rule 1: location/country/city keywords
        if any(k in fid for k in AUTOCOMPLETE_KEYWORDS) or \
           any(k in label for k in AUTOCOMPLETE_KEYWORDS):
            f["is_autocomplete"] = True
            continue

        # Rule 2: "question_*" id + Yes/No style label -> likely custom dropdown
        if fid.startswith("question_") and any(
            p in label for p in YESNO_LABEL_PATTERNS
        ):
            f["is_autocomplete"] = True
            continue

        f["is_autocomplete"] = False

    visible_fields = [
        f for f in fields
        if f.get("visible") and not f.get("is_demographic")
    ]

    print(f"[EXTRACT] Toplam: {len(fields)}, Gorunur: {len(visible_fields)}")
    for f in visible_fields:
        if f.get("is_autocomplete"):
            print(f"  [AUTOCOMPLETE] {f['id']} (label={f.get('label', '')[:40]})")
    return visible_fields


# ============================================================
# 3. LLM DECISION
# ============================================================

LLM_SYSTEM_PROMPT = """You are a form-filling assistant for job applications.

You receive:
1. A list of form fields (id, label, type, required, options)
2. The candidate's profile (personal info, experience, skills)
3. The job description (for context)

Your task: for EACH field, decide the correct ACTION and VALUE.

Available actions:
- "fill": type text into a plain input/textarea (no dropdown)
- "autocomplete": type text AND pick the first dropdown suggestion
  (USE THIS when "is_autocomplete": true, e.g. country, location, city)
- "select": choose an option from a native <select> element
- "upload": upload a file (value = "cv" or "cover_letter")
- "click": click the element (for checkboxes/radios)
- "skip": leave empty (for unclear/irrelevant fields)

Return ONLY valid JSON:
{
  "decisions": [
    {"id": "first_name", "action": "fill", "value": "Ahmet"},
    {"id": "country", "action": "select", "value": "NL"},
    {"id": "resume", "action": "upload", "value": "cv"},
    {"id": "question_123", "action": "fill", "value": "I want to join..."},
    {"id": "weird_field", "action": "skip", "value": ""}
  ]
}

RULES:
1. Standard fields:
   - first_name → candidate first name
   - last_name → candidate last name
   - email → candidate email
   - phone → candidate phone (with country code)
   - country → candidate country (use the option VALUE, not text)
   - location / candidate-location → candidate city + country
   - resume / cv → action "upload", value "cv"
   - cover / cover_letter → action "upload", value "cover_letter"

2. CRITICAL — For fields with "is_autocomplete": true:
   use action "autocomplete", NOT "fill". This makes Selenium:
   - type the value
   - wait for the dropdown
   - press Arrow Down + Enter to accept the first suggestion
   Examples:
     {"id": "country", "action": "autocomplete", "value": "Netherlands"}
     {"id": "candidate-location", "action": "autocomplete", "value": "Almelo"}

3. CRITICAL — If a field has an "options" array (native <select>):
   you MUST use action "select", NOT "fill".
   Use the option VALUE (not the visible text).
   Example: {"id": "question_xxx", "action": "select", "value": "Yes"}
   Common dropdowns: Yes/No questions, work authorization, pronouns.

3. For required open questions ("Why this company?", "Additional info"):
   Write a tailored, honest 2-4 sentence answer based on the candidate's
   real experience and the job description. NEVER invent achievements.

4. For "Are you authorized to work?" → "Yes"
   For "Have you worked here before?" → "No"
   For "Willing to relocate?" → "Open to relocating within the Netherlands"

5. For optional open questions (LinkedIn, pronouns, other website):
   - LinkedIn: use the candidate's LinkedIn URL if available, else skip
   - Pronouns: skip
   - Other website: skip

6. NEVER invent: skills, experience, dates, metrics, certifications.
   If unsure, use "skip".

7. If a field label is unclear or you cannot determine the correct value,
   use "skip" with empty value.

Return ONLY the JSON object. No markdown, no explanation.
"""


def ask_llm_to_fill(fields, profile, job_description, model="glm-5.3-flash"):
    """Send form structure + profile to LLM, get back decisions."""
    from openai import OpenAI

    api_key = os.environ.get("GLM_API_KEY")
    if not api_key:
        raise RuntimeError("GLM_API_KEY is not set")

    client = OpenAI(api_key=api_key, base_url="https://api.z.ai/api/paas/v4/")

    user_prompt = f"""# Form Fields
{json.dumps(fields, indent=2, ensure_ascii=False)}

# Candidate Profile
{json.dumps(profile, indent=2, ensure_ascii=False)}

# Job Description (first 2000 chars)
{job_description[:2000]}
"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
        max_tokens=4000,
        extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
    )

    content = response.choices[0].message.content
    result = json.loads(content)
    return result.get("decisions", [])


# ============================================================
# 4. APPLY DECISIONS
# ============================================================

def apply_decisions(driver, decisions, cv_path=None, cl_path=None):
    """Apply LLM's decisions to the form.

    Returns: dict with counts and details.
    """
    stats = {"fill": 0, "select": 0, "upload": 0, "click": 0, "skip": 0, "failed": 0}
    details = []

    for d in decisions:
        fid = d.get("id")
        action = d.get("action")
        value = d.get("value", "")

        if action == "skip" or not fid:
            stats["skip"] += 1
            continue

        try:
            el = driver.find_element(By.ID, fid)
        except Exception:
            try:
                el = driver.find_element(By.NAME, fid)
            except Exception:
                stats["failed"] += 1
                details.append(f"NOT FOUND: {fid}")
                continue

        try:
            # Scroll into view
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
            time.sleep(0.3)

            if action == "fill":
                # Special case: if value is Yes/No and element is a <select>,
                # use Select() instead of send_keys()
                if str(value).strip().lower() in ("yes", "no"):
                    try:
                        sel_obj = Select(el)
                        # Try by visible text
                        try:
                            sel_obj.select_by_visible_text(str(value))
                            stats["select"] = stats.get("select", 0) + 1
                            details.append(f"fill-as-select: {fid} = {value}")
                            continue
                        except Exception:
                            pass
                    except Exception:
                        pass
                el.clear()
                el.send_keys(str(value))
                stats["fill"] += 1
                details.append(f"fill: {fid} = {str(value)[:60]}")

            elif action == "autocomplete":
                import re as _re
                el.clear()
                el.click()
                time.sleep(0.5)
                el.send_keys(str(value))
                time.sleep(2.0)

                def _norm(t):
                    t = str(t or "").strip().lower()
                    t = _re.sub(r"\+\d+", "", t)
                    t = _re.sub(r"\s+", " ", t).strip()
                    return t.rstrip(",").strip()

                value_norm = _norm(value)

                all_opts = []
                for selector in [
                    "[role='option']",
                    "li[role='option']",
                    "ul li",
                    "[class*='option']",
                    "[class*='menu'] li",
                    "[class*='listbox'] li",
                ]:
                    try:
                        for opt in driver.find_elements(By.CSS_SELECTOR, selector):
                            try:
                                txt = (opt.text or "").strip()
                                if not txt:
                                    continue
                                if opt in all_opts:
                                    continue
                                all_opts.append(opt)
                            except Exception:
                                continue
                    except Exception:
                        continue

                scored = []
                for opt in all_opts:
                    txt_raw = (opt.text or "").strip()
                    txt = _norm(txt_raw)
                    if not txt:
                        continue
                    score = 0
                    if txt == value_norm:
                        score = 100
                    elif txt.startswith(value_norm):
                        score = 90
                    elif _re.search(r"\b" + _re.escape(value_norm) + r"\b", txt):
                        score = max(50, 70 - (len(txt) - len(value_norm)))
                    if score > 0:
                        scored.append((score, len(txt), opt, txt_raw))

                chosen_text = None
                if scored:
                    scored.sort(key=lambda x: (-x[0], x[1]))
                    chosen_text = scored[0][3]
                    details.append(
                        f"autocomplete-MATCH: {fid} -> '{chosen_text[:60]}' (score={scored[0][0]})"
                    )

                if chosen_text:
                    # Re-find by text (avoids stale element reference)
                    clicked_ok = False
                    try:
                        target_lower = chosen_text.strip().lower()
                        # Small wait so the dropdown settles
                        time.sleep(0.5)
                        for selector in [
                            "[role='option']",
                            "li[role='option']",
                            "ul li",
                            "[class*='option']",
                            "[class*='menu'] li",
                            "[class*='listbox'] li",
                        ]:
                            if clicked_ok:
                                break
                            try:
                                fresh_opts = driver.find_elements(By.CSS_SELECTOR, selector)
                                for opt in fresh_opts:
                                    try:
                                        opt_text = (opt.text or "").strip()
                                        if opt_text.lower() == target_lower:
                                            driver.execute_script(
                                                "arguments[0].scrollIntoView({block: 'center'});",
                                                opt,
                                            )
                                            time.sleep(0.2)
                                            opt.click()
                                            clicked_ok = True
                                            break
                                    except Exception:
                                        continue
                            except Exception:
                                continue
                    except Exception:
                        pass

                    if clicked_ok:
                        stats["autocomplete"] = stats.get("autocomplete", 0) + 1
                        details.append(f"autocomplete-CLICK: {fid} -> {chosen_text[:50]}")
                    else:
                        # Fallback: Arrow Down + Enter
                        ActionChains(driver).send_keys(Keys.ARROW_DOWN).send_keys(Keys.ENTER).perform()
                        stats["autocomplete"] = stats.get("autocomplete", 0) + 1
                        details.append(f"autocomplete-KEY: {fid} = {value}")
                else:
                    ActionChains(driver).send_keys(Keys.ARROW_DOWN).send_keys(Keys.ENTER).perform()
                    stats["autocomplete"] = stats.get("autocomplete", 0) + 1
                    details.append(f"autocomplete-KEY: {fid} = {value}")

                time.sleep(0.5)


            elif action == "select":
                sel = Select(el)
                # Try by value first, then by visible text
                try:
                    sel.select_by_value(str(value))
                except Exception:
                    sel.select_by_visible_text(str(value))
                stats["select"] += 1
                details.append(f"select: {fid} = {value}")

            elif action == "upload":
                path = None
                if value == "cv" and cv_path:
                    path = cv_path
                elif value == "cover_letter" and cl_path:
                    path = cl_path
                elif value and Path(str(value)).exists():
                    path = value

                if path and Path(path).exists():
                    el.send_keys(str(Path(path).resolve()))
                    stats["upload"] += 1
                    details.append(f"upload: {fid} = {Path(path).name}")
                    time.sleep(1.5)  # wait for upload to process
                else:
                    stats["failed"] += 1
                    details.append(f"upload skipped (no file): {fid}")

            elif action == "click":
                el.click()
                stats["click"] += 1
                details.append(f"click: {fid}")

        except Exception as e:
            stats["failed"] += 1
            details.append(f"ERROR {fid}: {e}")

    return {"stats": stats, "details": details}


# ============================================================
# 5. MAIN ENTRY
# ============================================================

def ai_fill_form(
    job_url,
    profile,
    job_description,
    cv_path=None,
    cl_path=None,
    headless=False,
    keep_open_seconds=600,
):
    """Full pipeline: open form → extract → LLM → apply → wait for review."""
    print(f"[1/5] Opening {job_url}")
    driver = build_driver(headless=headless)

    try:
        driver.get(job_url)
        time.sleep(4)

        print("[2/5] Extracting form fields")
        fields = extract_form_fields(driver)
        if not fields:
            return {"success": False, "message": "No form fields found"}

        print("[3/5] Asking LLM for decisions")
        decisions = ask_llm_to_fill(fields, profile, job_description)
        print(f"      LLM returned {len(decisions)} decisions")

        print("[4/5] Applying decisions to the form")
        result = apply_decisions(driver, decisions, cv_path=cv_path, cl_path=cl_path)

        print(f"[5/5] Form filled. Browser stays open for {keep_open_seconds}s.")
        print("      Review and click Apply manually.")
        time.sleep(keep_open_seconds)

        return {
            "success": True,
            "message": "Form filled. Review and submit manually.",
            "decisions": decisions,
            "apply_result": result,
        }
    except Exception as e:
        import traceback
        return {"success": False, "message": str(e), "trace": traceback.format_exc()}
    finally:
        driver.quit()
