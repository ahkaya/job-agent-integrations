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

    visible_fields = []
    for f in fields:
        if not f.get("visible"):
            continue
        if f.get("is_demographic"):
            continue
        fid = f.get("id") or ""
        # Dynamic auto_N ids: keep only if the field has a SHORT, meaningful label
        # (long labels usually mean the selector text got merged into the label,
        # e.g. "What is your location?Select...AfghanistanAlbania...")
        if fid.startswith("auto_"):
            label = (f.get("label") or "").strip()
            placeholder = (f.get("placeholder") or "").strip()
            has_short_label = 0 < len(label) <= 100
            has_placeholder = bool(placeholder)
            if not (has_short_label or has_placeholder):
                continue
        visible_fields.append(f)

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
   You MUST use action "autocomplete", NOT "fill". This is a hard rule.
   The "autocomplete" action tells Selenium to:
   - type the value
   - wait for the dropdown
   - click the best matching suggestion
   Examples:
     {"id": "country", "action": "autocomplete", "value": "Netherlands"}
     {"id": "candidate-location", "action": "autocomplete", "value": "Almelo"}
     {"id": "location-input", "action": "autocomplete", "value": "Almelo"}
   NEVER use "fill" when is_autocomplete is true.

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

6. CURRENT EMPLOYMENT RULE (STRICT):
   - Fields named "Current Company", "Current Employer", "org", "company":
     The candidate is NOT currently employed. All their roles have end dates
     (the most recent is "2021 - 2025"). So for these fields:
     -> action MUST be "skip" with EMPTY value.
     -> Do NOT write any company name here, even if it's the most recent one.
   - Fields named "Current Title", "Current Position":
     -> action MUST be "skip" with EMPTY value.
   - If the form has a "Most Recent Company" or "Previous Company" field,
     you MAY fill it with the most recent employer (Taymada Cosmetics).

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

    Two-pass approach:
      Pass 1: Upload files (CV, cover letter) — this may trigger ATS-side
              autofill from the resume (e.g. Lever parses the PDF and fills
              name/email/phone/current-company automatically).
      Wait 8s for the ATS parser to finish.
      Pass 2: Apply all other decisions, overwriting anything the ATS filled.
              Also clear any field the LLM marked as "skip".
    """
    import re as _re

    stats = {"fill": 0, "autocomplete": 0, "select": 0, "upload": 0,
             "click": 0, "skip": 0, "clear": 0, "failed": 0}
    details = []

    def _norm(t):
        t = str(t or "").strip().lower()
        t = _re.sub(r"\+\d+", "", t)
        t = _re.sub(r"\s+", " ", t).strip()
        return t.rstrip(",").strip()

    # ---------------- PASS 1: uploads ----------------
    uploads = [d for d in decisions if d.get("action") == "upload"]
    others = [d for d in decisions if d.get("action") != "upload"]

    for d in uploads:
        fid = d.get("id")
        value = d.get("value", "")
        if not fid:
            continue
        try:
            el = driver.find_element(By.ID, fid)
        except Exception:
            try:
                el = driver.find_element(By.NAME, fid)
            except Exception:
                # Fallback: first visible file input
                try:
                    for fi in driver.find_elements(By.CSS_SELECTOR, "input[type=file]"):
                        if fi.get_attribute("name") == value or "resume" in (fi.get_attribute("name") or ""):
                            el = fi
                            break
                    else:
                        stats["failed"] += 1
                        details.append(f"upload NOT FOUND: {fid}")
                        continue
                except Exception:
                    stats["failed"] += 1
                    details.append(f"upload NOT FOUND: {fid}")
                    continue

        path = None
        if value == "cv" and cv_path:
            path = cv_path
        elif value == "cover_letter" and cl_path:
            path = cl_path
        elif value and Path(str(value)).exists():
            path = value

        if not path or not Path(path).exists():
            stats["failed"] += 1
            details.append(f"upload skipped (no file): {fid}")
            continue

        try:
            el.send_keys(str(Path(path).resolve()))
            stats["upload"] += 1
            details.append(f"upload: {fid} = {Path(path).name}")
        except Exception as e:
            # Try any file input
            uploaded = False
            try:
                for fi in driver.find_elements(By.CSS_SELECTOR, "input[type=file]"):
                    fi.send_keys(str(Path(path).resolve()))
                    uploaded = True
                    break
            except Exception:
                pass
            if uploaded:
                stats["upload"] += 1
                details.append(f"upload (fallback): {fid} = {Path(path).name}")
            else:
                stats["failed"] += 1
                details.append(f"upload failed: {fid} - {e}")

    # Wait for ATS-side resume parsing / autofill
    if uploads:
        print("[apply] Waiting 8s for ATS resume parsing...")
        time.sleep(8)
        print("[apply] ATS autofill complete. Applying LLM decisions only to empty fields.")

    # ---------------- PASS 2: everything else ----------------
    for d in others:
        fid = d.get("id")
        action = d.get("action")
        value = d.get("value", "")

        # Current employment fields: the candidate is NOT currently employed.
        # Lever's CV parser wrongly writes the most recent employer.
        # We replace it with "Not currently employed" (or "N/A" for short inputs).
        fid_lower = (fid or "").lower()
        if fid_lower in ("org", "current_company", "currentcompany") or \
           "current company" in fid_lower or "current employer" in fid_lower:
            replacement = "Not currently employed"
            done = False
            for finder in (lambda: driver.find_element(By.ID, fid),
                           lambda: driver.find_element(By.NAME, fid)):
                try:
                    target = finder()
                    # Try maxlength first
                    try:
                        maxlen = target.get_attribute("maxlength")
                        if maxlen and int(maxlen) < len(replacement):
                            replacement = "N/A"
                    except Exception:
                        pass
                    driver.execute_script("""
                        const el = arguments[0];
                        const value = arguments[1];
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        setter.call(el, value);
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    """, target, replacement)
                    done = True
                    break
                except Exception:
                    continue

            if done:
                stats["fill"] = stats.get("fill", 0) + 1
                details.append(f"replaced (current employment): {fid} = '{replacement}'")
            else:
                stats["skip"] += 1
                details.append(f"skip (current employment): {fid}")
            continue


        if action == "skip" or not fid:
            # If a previously-filled field is being skipped, clear it
            if action == "skip" and fid:
                try:
                    el = driver.find_element(By.ID, fid)
                    existing = el.get_attribute("value") or ""
                    if existing.strip():
                        el.clear()
                        stats["clear"] += 1
                        details.append(f"cleared (LLM skip): {fid} (was: {existing[:30]})")
                    else:
                        stats["skip"] += 1
                        details.append(f"skip: {fid}")
                except Exception:
                    try:
                        el = driver.find_element(By.NAME, fid)
                        existing = el.get_attribute("value") or ""
                        if existing.strip():
                            el.clear()
                            stats["clear"] += 1
                            details.append(f"cleared (LLM skip): {fid}")
                        else:
                            stats["skip"] += 1
                    except Exception:
                        stats["skip"] += 1
            else:
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

        # SKIP IF ALREADY FILLED BY THE ATS (e.g. Lever parses the CV
        # and fills name / email / phone / location correctly).
        # Read the current value via 3 strategies (React inputs do not
        # always expose `value` as an attribute).
        if action in ("fill", "autocomplete"):
            existing_value = ""
            try:
                existing_value = (el.get_attribute("value") or "").strip()
            except Exception:
                pass
            if not existing_value:
                try:
                    existing_value = (el.get_property("value") or "").strip()
                except Exception:
                    pass
            if not existing_value:
                try:
                    existing_value = (driver.execute_script(
                        "return arguments[0].value || '';", el
                    ) or "").strip()
                except Exception:
                    pass

            if existing_value:
                details.append(
                    f"skip (already filled by ATS): {fid} = '{existing_value[:40]}'"
                )
                stats["skip"] += 1
                continue

        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
            time.sleep(0.3)

            if action == "fill":
                # Special case 1: Yes/No + native select
                if str(value).strip().lower() in ("yes", "no"):
                    try:
                        sel_obj = Select(el)
                        try:
                            sel_obj.select_by_visible_text(str(value))
                            stats["select"] = stats.get("select", 0) + 1
                            details.append(f"fill-as-select: {fid} = {value}")
                            continue
                        except Exception:
                            pass
                    except Exception:
                        pass

                # Special case 2: field is autocomplete → force autocomplete
                if d.get("_is_autocomplete"):
                    details.append(f"fill->autocomplete override: {fid}")
                    el.clear()
                    el.click()
                    time.sleep(0.5)
                    el.send_keys(str(value))
                    time.sleep(2.0)

                    value_norm = _norm(value)
                    all_opts = []
                    for selector in [
                        "[role='option']", "li[role='option']", "ul li",
                        "[class*='option']", "[class*='menu'] li",
                        "[class*='listbox'] li", "ul[class*='results'] li",
                    ]:
                        try:
                            for opt in driver.find_elements(By.CSS_SELECTOR, selector):
                                try:
                                    if not (opt.text or "").strip():
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
                        raw = (opt.text or "").strip()
                        t = _norm(raw)
                        if not t:
                            continue
                        sc = 0
                        if t == value_norm:
                            sc = 100
                        elif t.startswith(value_norm):
                            sc = 90
                        else:
                            # City-name match (e.g. "Almelo, Netherlands" vs "Almelo, NLD")
                            v_first = value_norm.split()[0] if value_norm else ""
                            t_first = t.split()[0] if t else ""
                            if v_first and t_first and v_first == t_first:
                                sc = 85
                            elif _re.search(r"\b" + _re.escape(value_norm) + r"\b", t):
                                sc = max(50, 70 - (len(t) - len(value_norm)))
                        if sc > 0:
                            scored.append((sc, len(t), opt, raw))

                    chosen_txt = None
                    if scored:
                        scored.sort(key=lambda x: (-x[0], x[1]))
                        chosen_txt = scored[0][3]
                        details.append(
                            f"autocomplete-MATCH: {fid} -> '{chosen_txt[:60]}' (score={scored[0][0]})"
                        )

                    clicked = False
                    if chosen_txt:
                        # JS click (stale-proof)
                        try:
                            js_clicked = driver.execute_script("""
                                const target = arguments[0].toLowerCase().trim();
                                const selectors = [
                                    "[role='option']", "li[role='option']", "ul li",
                                    "[class*='option']", "[class*='menu'] li",
                                    "[class*='listbox'] li", "ul[class*='results'] li"
                                ];
                                for (const sel of selectors) {
                                    for (const el of document.querySelectorAll(sel)) {
                                        if (el.textContent.trim().toLowerCase() === target) {
                                            el.click();
                                            return el.textContent.trim();
                                        }
                                    }
                                }
                                return null;
                            """, chosen_txt)
                            if js_clicked:
                                clicked = True
                                stats["autocomplete"] += 1
                                details.append(f"autocomplete-JSCLICK: {fid} -> {js_clicked[:50]}")
                        except Exception:
                            pass

                        if not clicked:
                            try:
                                el.send_keys(Keys.TAB)
                                time.sleep(0.5)
                                clicked = True
                                stats["autocomplete"] += 1
                                details.append(f"autocomplete-TAB: {fid} = {value}")
                            except Exception:
                                pass
                        if not clicked:
                            ActionChains(driver).send_keys(Keys.ARROW_DOWN).send_keys(Keys.ENTER).perform()
                            stats["autocomplete"] += 1
                            details.append(f"autocomplete-KEY: {fid} = {value}")
                    else:
                        try:
                            el.send_keys(Keys.TAB)
                            time.sleep(0.5)
                            stats["autocomplete"] += 1
                            details.append(f"autocomplete-TAB: {fid} = {value}")
                        except Exception:
                            ActionChains(driver).send_keys(Keys.ARROW_DOWN).send_keys(Keys.ENTER).perform()
                            stats["autocomplete"] += 1
                            details.append(f"autocomplete-KEY: {fid} = {value}")
                    time.sleep(0.5)
                    continue

                el.clear()
                el.send_keys(str(value))
                stats["fill"] += 1
                details.append(f"fill: {fid} = {str(value)[:60]}")

            elif action == "autocomplete":
                el.clear()
                el.click()
                time.sleep(0.5)
                el.send_keys(str(value))
                time.sleep(2.0)
                value_norm = _norm(value)

                all_opts = []
                for selector in [
                    "[role='option']", "li[role='option']", "ul li",
                    "[class*='option']", "[class*='menu'] li",
                    "[class*='listbox'] li", "ul[class*='results'] li",
                    "div[role='listbox'] div", ".dropdown-menu li",
                    ".autocomplete-results li", "li[class*='item']",
                ]:
                    try:
                        for opt in driver.find_elements(By.CSS_SELECTOR, selector):
                            try:
                                if not (opt.text or "").strip():
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
                    raw = (opt.text or "").strip()
                    t = _norm(raw)
                    if not t:
                        continue
                    sc = 0
                    if t == value_norm:
                        sc = 100
                    elif t.startswith(value_norm):
                        sc = 90
                    else:
                        v_first = value_norm.split()[0] if value_norm else ""
                        t_first = t.split()[0] if t else ""
                        if v_first and t_first and v_first == t_first:
                            sc = 85
                        elif _re.search(r"\b" + _re.escape(value_norm) + r"\b", t):
                            sc = max(50, 70 - (len(t) - len(value_norm)))
                    if sc > 0:
                        scored.append((sc, len(t), opt, raw))

                chosen_txt = None
                if scored:
                    scored.sort(key=lambda x: (-x[0], x[1]))
                    chosen_txt = scored[0][3]
                    details.append(
                        f"autocomplete-MATCH: {fid} -> '{chosen_txt[:60]}' (score={scored[0][0]})"
                    )

                clicked = False
                if chosen_txt:
                    try:
                        js_clicked = driver.execute_script("""
                            const target = arguments[0].toLowerCase().trim();
                            const selectors = [
                                "[role='option']", "li[role='option']", "ul li",
                                "[class*='option']", "[class*='menu'] li",
                                "[class*='listbox'] li", "ul[class*='results'] li"
                            ];
                            for (const sel of selectors) {
                                for (const el of document.querySelectorAll(sel)) {
                                    if (el.textContent.trim().toLowerCase() === target) {
                                        el.click();
                                        return el.textContent.trim();
                                    }
                                }
                            }
                            return null;
                        """, chosen_txt)
                        if js_clicked:
                            clicked = True
                            stats["autocomplete"] += 1
                            details.append(f"autocomplete-JSCLICK: {fid} -> {js_clicked[:50]}")
                    except Exception:
                        pass

                    if not clicked:
                        try:
                            el.send_keys(Keys.TAB)
                            time.sleep(0.5)
                            clicked = True
                            stats["autocomplete"] += 1
                            details.append(f"autocomplete-TAB: {fid} = {value}")
                        except Exception:
                            pass
                    if not clicked:
                        ActionChains(driver).send_keys(Keys.ARROW_DOWN).send_keys(Keys.ENTER).perform()
                        stats["autocomplete"] += 1
                        details.append(f"autocomplete-KEY: {fid} = {value}")
                else:
                    try:
                        el.send_keys(Keys.TAB)
                        time.sleep(0.5)
                        stats["autocomplete"] += 1
                        details.append(f"autocomplete-TAB: {fid} = {value}")
                    except Exception:
                        ActionChains(driver).send_keys(Keys.ARROW_DOWN).send_keys(Keys.ENTER).perform()
                        stats["autocomplete"] += 1
                        details.append(f"autocomplete-KEY: {fid} = {value}")
                time.sleep(0.5)

            elif action == "select":
                sel_obj = Select(el)
                try:
                    sel_obj.select_by_value(str(value))
                except Exception:
                    sel_obj.select_by_visible_text(str(value))
                stats["select"] += 1
                details.append(f"select: {fid} = {value}")

            elif action == "click":
                el.click()
                stats["click"] += 1
                details.append(f"click: {fid}")

        except Exception as e:
            stats["failed"] += 1
            details.append(f"ERROR {fid}: {e}")

    return {"stats": stats, "details": details}


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

        # Inject is_autocomplete flag from extracted fields into each decision
        field_map = {f["id"]: f for f in fields}
        for d in decisions:
            src_field = field_map.get(d.get("id"))
            if src_field and src_field.get("is_autocomplete"):
                d["_is_autocomplete"] = True

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
