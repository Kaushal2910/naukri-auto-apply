"""
Naukri auto-apply. Requires session_naukri.json from login_capture.py.

Usage:
    python naukri_apply.py
"""
import csv
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from common.profile import Profile
from common import llm
from common import learned_answers
from common.human_input import ask_user

SESSION_FILE = "session_naukri.json"
LOG_FILE = "applications_log.csv"

STOP_PHRASES = [
    "too many requests", "unusual activity", "verify you are human",
    "captcha", "temporarily blocked", "please try again later and reduce",
    "there was an error while processing your request",
    # Akamai edge block page (Naukri serves this to headless/bot traffic):
    # "Access Denied / You don't have permission to access ... on this server."
    "access denied", "you don't have permission to access",
]

CARD_SELECTORS = [
    ".srp-jobtuple-wrapper[data-job-id]",
    "article.jobTuple",
    "article[data-job-id]",
    "div[data-job-id]",
]

NO_RESULTS_HINTS = [
    "no results found", "no jobs found", "0 jobs", "try different keywords"
]

SENSITIVE_FIELD_HINTS = [
    "date of birth", "dob", "pan number", "pan card", "aadhar", "aadhaar",
    "passport", "bank account", "ifsc", "father's name", "father name",
    "mother's name", "mother name", "marital status", "blood group",
    "emergency contact", "voter id", "driving licence", "driving license",
]

TRIGGER_PHRASES = {
    "years_experience": ["years of experience", "total experience", "how many years", "work experience"],
    "notice_period": ["notice period"],
    "current_ctc": ["current ctc", "current salary", "current compensation", "present ctc", "present salary"],
    "expected_ctc": ["expected ctc", "expected salary", "expected compensation"],
    "current_city": ["current city", "current location", "which city", "current place"],
    "relocate": ["relocate", "relocation", "willing to move"],
    "night_shift": ["night shift"],
    "weekend_work": ["weekend"],
}


def log_row(row: list):
    new_file = not Path(LOG_FILE).exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "source", "title", "company", "status", "reason"])
        w.writerow(row)


def passes_filters(card: dict, profile: Profile, role: str) -> tuple[bool, str]:
    title = (card.get("title") or "").lower()
    company = (card.get("company") or "").lower()

    required_keywords = profile.data.get("role_required_keywords", {}).get(role)
    if not required_keywords and role not in profile.data.get("role_required_keywords", {}):
        generic = {"engineer", "developer", "administrator", "analyst", "senior", "junior", "lead"}
        required_keywords = [w for w in role.lower().split() if w not in generic]
        if not required_keywords:
            required_keywords = role.lower().split()

    if required_keywords and not any(kw.lower() in title for kw in required_keywords):
        return False, f"title doesn't match role keywords ({role})"

    for excl in profile.company_exclude:
        if excl.lower() in company:
            return False, f"company excluded ({excl})"

    if profile.company_include_only:
        if not any(inc.lower() in company for inc in profile.company_include_only):
            return False, "not in include-only list"

    exp_text = card.get("exp") or ""
    digits = [int(s) for s in exp_text.replace("Yrs", "").replace("yrs", "").replace("-", " ").split() if s.isdigit()]
    if len(digits) >= 2:
        lo, hi = digits[0], digits[-1]
        if hi < profile.seniority_floor_years or lo > profile.seniority_ceiling_years:
            return False, f"experience range mismatch ({exp_text})"

    return True, ""


def safe_evaluate(page, script, arg=None, default=None):
    try:
        return page.evaluate(script, arg) if arg is not None else page.evaluate(script)
    except Exception as e:
        print(f"  (page.evaluate failed, continuing anyway: {e})")
        return default


def _scroll_and_wait(page, rounds: int = 4):
    for _ in range(rounds):
        safe_evaluate(page, "window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.2)


def _extract_cards_with_fallback(page):
    js = """
    (selectors) => {
      const out = [];
      const seen = new Set();

      function pick(el, sels){
        for (const s of sels){
          const n = el.querySelector(s);
          if (n && (n.innerText || n.textContent)) return (n.innerText || n.textContent).trim();
        }
        return "";
      }

      function href(el){
        const a = el.querySelector('a.title, a[href*="/job-listings"], a[href*="naukri.com/job-listings"], a');
        return a ? a.href : "";
      }

      for (const sel of selectors){
        const nodes = Array.from(document.querySelectorAll(sel));
        for (const c of nodes){
          const jobId = c.getAttribute("data-job-id")
            || c.getAttribute("id")
            || c.querySelector("[data-job-id]")?.getAttribute("data-job-id")
            || "";
          const link = href(c);
          const title = pick(c, ['a.title', 'h2 a', 'a[title]', '.title', 'h2']);
          const company = pick(c, ['a.comp-name', '.comp-name', '.comp-dtls-wrap a', '.companyName', '.subtitle']);
          const exp = pick(c, ['.expwdth', '.exp', '[class*="experience"]']);
          const key = (jobId || link || title).trim();
          if (!key) continue;
          if (seen.has(key)) continue;
          seen.add(key);
          out.push({
            jobId: jobId || link || key,
            title: title || "",
            href: link || "",
            company: company || "",
            exp: exp || "",
          });
        }
      }

      return out.filter(c => c.title && c.href);
    }
    """
    return safe_evaluate(page, js, arg=CARD_SELECTORS, default=[]) or []


def enumerate_cards(page):
    return _extract_cards_with_fallback(page)


def _attach_jobs_api_listener(page, bucket: list):
    def on_response(resp):
        try:
            url = resp.url.lower()
            ctype = (resp.headers.get("content-type") or "").lower()
            if "application/json" not in ctype:
                return
            if "naukri" in url and ("job" in url or "search" in url or "listing" in url):
                data = resp.json()
                bucket.append({"url": resp.url, "data": data, "status": resp.status})
        except Exception:
            return

    page.on("response", on_response)


def _count_jobs_from_api_bucket(bucket: list[dict[str, Any]]) -> int:
    count = 0
    for item in bucket[-30:]:
        data = item.get("data")
        if isinstance(data, dict):
            for k in ("jobs", "jobDetails", "results", "data", "items"):
                v = data.get(k)
                if isinstance(v, list):
                    count += len(v)
    return count


def _dump_page_artifacts(page, tag: str):
    debug_dir = Path("debug_screenshots")
    debug_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_tag = "".join(c if c.isalnum() else "_" for c in tag)[:80]
    png = debug_dir / f"{safe_tag}_{ts}.png"
    html = debug_dir / f"{safe_tag}_{ts}.html"
    txt = debug_dir / f"{safe_tag}_{ts}.txt"

    try:
        page.screenshot(path=str(png), full_page=True)
    except Exception as e:
        print(f"  (couldn't save debug screenshot: {e})")
    try:
        html.write_text(page.content(), encoding="utf-8")
    except Exception as e:
        print(f"  (couldn't save debug HTML: {e})")
    try:
        title = page.title()
    except Exception:
        title = ""
    try:
        body_text = page.inner_text("body")
    except Exception:
        body_text = ""

    txt.write_text(f"URL: {page.url}\nTITLE: {title}\n\n{(body_text or '')[:5000]}", encoding="utf-8")
    return str(png), str(html), str(txt)


def page_has_stop_signal(page) -> str | None:
    try:
        text = page.inner_text("body").lower()
    except Exception:
        return None
    for phrase in STOP_PHRASES:
        if phrase in text:
            return phrase
    if "naukri.com/nlogin" in page.url:
        return "session expired / login prompt"
    return None


def _page_looks_blocked_or_empty(page) -> tuple[str, str]:
    stop = page_has_stop_signal(page)
    if stop:
        return "stop", stop

    try:
        body = page.inner_text("body").lower()
    except Exception:
        body = ""

    if any(h in body for h in NO_RESULTS_HINTS):
        return "empty", "site reports no results"

    if len((body or "").strip()) < 200:
        return "unknown", "very small body text; possible blocked/interstitial shell"

    return "ok", ""


def check_apply_button(page) -> str:
    if page.query_selector('#company-site-button'):
        return "external"
    if page.query_selector('#apply-button'):
        return "native"
    return "none"


def _wait_send_enabled(page, timeout_ms: int = 4000) -> bool:
    waited = 0
    step = 300
    while waited <= timeout_ms:
        enabled = safe_evaluate(page, """
            () => {
                const wrapper = document.querySelector('[id^="sendMsg__"]');
                if (!wrapper) return true;
                return !wrapper.className.includes('disabled');
            }
        """, default=True)
        if enabled:
            return True
        time.sleep(step / 1000)
        waited += step
    return False


def _js_click_send(page) -> bool:
    return safe_evaluate(page, """
        () => {
            const el = document.querySelector('.sendMsg');
            if (!el) return false;
            el.click();
            return true;
        }
    """, default=False)


def click_native_apply(page) -> bool:
    return bool(safe_evaluate(page, """
        () => {
            const btn = document.getElementById('apply-button');
            if (btn) { btn.click(); return true; }
            return false;
        }
    """, default=False))


def _save_debug_screenshot(page, job_title: str) -> str:
    debug_dir = Path("debug_screenshots")
    debug_dir.mkdir(exist_ok=True)
    safe_name = "".join(c if c.isalnum() else "_" for c in (job_title or "unknown"))[:60]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_path = debug_dir / f"{safe_name}_{timestamp}.png"
    html_path = debug_dir / f"{safe_name}_{timestamp}.html"
    try:
        page.screenshot(path=str(png_path))
    except Exception as e:
        print(f"  (couldn't save debug screenshot: {e})")
    try:
        html_path.write_text(page.content())
    except Exception as e:
        print(f"  (couldn't save debug HTML: {e})")
    return str(png_path)


def verify_applied(page) -> bool:
    try:
        page.wait_for_timeout(3000)
    except Exception:
        pass
    try:
        text = page.inner_text("body").lower()
    except Exception:
        return False
    success_phrases = [
        'applied to "', "application sent", "successfully applied",
        "you have applied", "applied successfully",
    ]
    if any(p in text for p in success_phrases):
        return True
    btn = page.query_selector('#apply-button')
    if btn:
        try:
            label = btn.inner_text().strip().lower()
            if label and label != "apply":
                return True
        except Exception:
            pass
    return False


class StopRun(Exception):
    pass


class SkipJob(Exception):
    pass


def _get_options(page) -> list[dict]:
    return safe_evaluate(page, """
        () => {
            const root = document.querySelector('[class*="chatbot_Drawer"]') || document;
            const inputs = Array.from(root.querySelectorAll('input[type=radio], input[type=checkbox]'));
            return inputs.map((el, idx) => {
                let label = '';
                if (el.id) {
                    const lbl = root.querySelector(`label[for="${el.id}"]`);
                    if (lbl) label = lbl.innerText.trim();
                }
                if (!label) {
                    const parentLabel = el.closest('label');
                    if (parentLabel) label = parentLabel.innerText.trim();
                }
                if (!label && el.nextElementSibling) {
                    label = (el.nextElementSibling.innerText || '').trim();
                }
                return {idx, label, type: el.type};
            });
        }
    """, default=[]) or []


def _click_option(page, idx: int) -> bool:
    return safe_evaluate(page, """
        (idx) => {
            const root = document.querySelector('[class*="chatbot_Drawer"]') || document;
            const inputs = Array.from(root.querySelectorAll('input[type=radio], input[type=checkbox]'));
            if (inputs[idx]) { inputs[idx].click(); return true; }
            return false;
        }
    """, arg=idx, default=False)


def _is_sensitive_field(question: str) -> bool:
    lower_q = question.lower()
    return any(hint in lower_q for hint in SENSITIVE_FIELD_HINTS)


def _auto_decide_option(question: str, options: list[dict], profile: Profile) -> dict | None:
    lower_q = question.lower()
    by_label = {o["idx"]: o["label"].strip().lower() for o in options}

    def find_by_text(text: str):
        for idx, label in by_label.items():
            if label == text.lower():
                return next(o for o in options if o["idx"] == idx)
        return None

    rules = [
        (["relocate", "relocation", "willing to move"], "Yes" if profile.data.get("relocate_cities") else "No"),
        (["night shift"], "Yes" if profile.data.get("night_shift_ok") else "No"),
        (["weekend"], "Yes" if profile.data.get("weekend_ok") else "No"),
        (["immediately available", "immediate joiner"], "Yes" if profile.data.get("immediately_available") else "No"),
        (["currently employed", "currently working"], "Yes" if profile.data.get("current_employer") else "No"),
    ]
    for triggers, desired in rules:
        if any(t in lower_q for t in triggers):
            match = find_by_text(desired)
            if match:
                return match
    return None


def _handle_options_question(page, question: str, profile: Profile, timeout_s: int) -> bool:
    options = _get_options(page)
    if not options:
        return False

    stored = learned_answers.get_answer(question)
    if stored:
        for opt in options:
            if opt["label"].strip().lower() == stored.strip().lower():
                _click_option(page, opt["idx"])
                return True

    auto = _auto_decide_option(question, options, profile)
    if auto:
        _click_option(page, auto["idx"])
        learned_answers.save_answer(question, auto["label"])
        return True

    options_text = "\n".join(f"  {o['idx']}: {o['label']}" for o in options)
    response = ask_user(
        f"Screening question needs a choice:\n{question}\n\nOptions:\n{options_text}\n"
        f"Type the number of your choice (or numbers separated by commas for multi-select):",
        timeout_seconds=timeout_s,
    )
    if response is None:
        raise SkipJob(f"no response for options question: {question[:120]}")

    chosen_raw = [r.strip() for r in response.split(",") if r.strip()]
    matches = []
    for r in chosen_raw:
        try:
            idx = int(r)
            match = next((o for o in options if o["idx"] == idx), None)
        except ValueError:
            match = next((o for o in options if o["label"].strip().lower() == r.lower()), None)
        if match:
            matches.append(match)

    if not matches:
        raise SkipJob(f"couldn't match your response '{response}' to an option: {question[:120]}")

    for match in matches:
        _click_option(page, match["idx"])
    learned_answers.save_answer(question, matches[0]["label"] if len(matches) == 1 else response)
    return True


COMPLETION_PHRASES = [
    "thank you", "thanks for your response", "thanks for your time",
    "we will get back", "responses have been recorded", "no further questions",
    "application submitted", "that's all", "all the information we need",
]


def _is_completion_message(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in COMPLETION_PHRASES)


def _has_answerable_input(page, timeout_ms: int = 3000) -> bool:
    waited = 0
    step = 300
    while waited <= timeout_ms:
        found = safe_evaluate(page, """
            () => !!(document.querySelector('[id^="userInput"], [contenteditable="true"]') ||
                     document.querySelector('input[type=radio], input[type=checkbox]'))
        """, default=False)
        if found:
            return True
        time.sleep(step / 1000)
        waited += step
    return False


def _read_filled_text(page) -> str:
    return safe_evaluate(page, """
        () => {
            const ed = document.querySelector('[id^="userInput"], [contenteditable="true"]');
            return ed ? (ed.innerText || ed.textContent || '').trim() : '';
        }
    """, default="") or ""


def _fill_freetext(page, text: str):
    safe_evaluate(page, """
        (text) => {
            const ed = document.querySelector('[id^="userInput"], [contenteditable="true"]');
            if (!ed) return;
            ed.focus();
            document.execCommand('insertText', false, text);
        }
    """, arg=text)


def _fill_and_send(page, text: str, question: str):
    _fill_freetext(page, text)
    time.sleep(0.4)
    filled = _read_filled_text(page)
    if not filled:
        _fill_freetext(page, text)
        time.sleep(0.6)
        filled = _read_filled_text(page)
    if not filled:
        raise SkipJob(f"couldn't confirm the answer registered before sending: {question[:120]}")
    _wait_send_enabled(page)
    _js_click_send(page)
    time.sleep(2.0)


def answer_screening_chat(page, profile: Profile, job_context: str, timeout_s: int):
    answers = profile.answer_library()
    try:
        page.wait_for_selector(
            '[contenteditable="true"], [contenteditable=""], '
            'input[type=radio], input[type=checkbox], .botMsg',
            timeout=4000,
        )
    except PWTimeout:
        return

    for _ in range(15):
        stop = page_has_stop_signal(page)
        if stop:
            raise StopRun(f"stop signal during screening chat: {stop}")

        bubbles = page.query_selector_all('.botMsg')
        if not bubbles:
            break
        try:
            question = bubbles[-1].inner_text().strip()
        except Exception:
            break
        if not question:
            break

        if _is_completion_message(question):
            time.sleep(1.5)
            break

        if not _has_answerable_input(page):
            break

        options = _get_options(page)
        if options:
            _handle_options_question(page, question, profile, timeout_s)
            if not _wait_send_enabled(page):
                raise SkipJob(f"Send button stayed disabled after selecting an option: {question[:120]}")
            _js_click_send(page)
            time.sleep(2.0)
            continue

        stored = learned_answers.get_answer(question)
        if stored:
            _fill_and_send(page, stored, question)
            continue

        predefined = profile.get_predefined_answer(question)
        if predefined:
            _fill_and_send(page, predefined, question)
            learned_answers.save_answer(question, predefined)
            continue

        lower_q = question.lower()
        answered = False
        for key, phrases in TRIGGER_PHRASES.items():
            if key not in answers:
                continue
            if any(phrase in lower_q for phrase in phrases):
                _fill_and_send(page, str(answers[key]), question)
                answered = True
                break
        if answered:
            continue

        if _is_sensitive_field(question):
            response = ask_user(f"Screening question (personal detail):\n{question}", timeout_seconds=timeout_s)
            if response is None:
                raise SkipJob(f"no response for sensitive field: {question[:120]}")
            _fill_and_send(page, response, question)
            learned_answers.save_answer(question, response)
            continue

        draft = llm.draft_answer(question, profile.data, job_context)
        if draft.startswith("[NEEDS_HUMAN_INPUT"):
            response = ask_user(f"Screening question (AI couldn't answer from your profile):\n{question}",
                                timeout_seconds=timeout_s)
            if response is None:
                raise SkipJob(f"no response for: {question[:120]}")
            _fill_and_send(page, response, question)
            learned_answers.save_answer(question, response)
        else:
            _fill_and_send(page, draft, question)


def run():
    profile = Profile.load()
    if not Path(SESSION_FILE).exists():
        raise SystemExit(f"{SESSION_FILE} not found. Run: python login_capture.py naukri")

    human_timeout = profile.data.get("human_input_timeout_seconds", 120)
    max_pages = profile.data.get("max_pages_per_role", 5)

    applied = 0
    with sync_playwright() as p:
        browser_mode = profile.data.get("browser_mode", "visible")
        if browser_mode == "headless":
            browser = p.chromium.launch(headless=True, slow_mo=150)
        elif browser_mode == "minimized":
            browser = p.chromium.launch(headless=False, slow_mo=150, args=["--start-minimized"])
        else:
            browser = p.chromium.launch(headless=False, slow_mo=150)

        context = browser.new_context(storage_state=SESSION_FILE)
        page = context.new_page()

        api_bucket: list[dict[str, Any]] = []
        _attach_jobs_api_listener(page, api_bucket)

        for role in profile.target_roles:
            if applied >= profile.stop_after_n_applications:
                break

            slug = role.lower().replace(" ", "-")
            exp_param = int(profile.total_experience_years)
            seen_job_ids = set()
            stopped_entirely = False

            for page_no in range(1, max_pages + 1):
                if applied >= profile.stop_after_n_applications:
                    break

                page_suffix = "" if page_no == 1 else f"-{page_no}"
                url = (
                    f"https://www.naukri.com/{slug}-jobs{page_suffix}"
                    f"?experience={exp_param}"
                    f"&jobAge={profile.job_freshness_days}"
                )

                print(f"\n--- Searching: {role}, page {page_no} ({url}) ---")

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception as e:
                    print(f"  (navigation error, treating as end of results for this role: {e})")
                    break

                _scroll_and_wait(page, rounds=4)

                stop = page_has_stop_signal(page)
                if stop:
                    print(f"STOPPING: {stop}")
                    log_row([datetime.now(), "naukri", "-", "-", "stopped", stop])
                    stopped_entirely = True
                    break

                cards = enumerate_cards(page)

                if not cards:
                    time.sleep(2.0)
                    _scroll_and_wait(page, rounds=3)
                    cards = enumerate_cards(page)

                api_jobs_seen = _count_jobs_from_api_bucket(api_bucket)
                kind, reason = _page_looks_blocked_or_empty(page)

                if not cards:
                    png, html, txt = _dump_page_artifacts(page, f"empty_{role}_p{page_no}")
                    print(f"0 cards after fallback. kind={kind}, reason={reason}, api_jobs_seen={api_jobs_seen}")
                    print(f"Artifacts: {png} | {html} | {txt}")

                    if kind == "stop":
                        print(f"STOPPING: {reason}")
                        log_row([datetime.now(), "naukri", "-", "-", "stopped", reason])
                        stopped_entirely = True
                        break

                    if api_jobs_seen > 0:
                        log_row([
                            datetime.now(), "naukri", "-", "-", "skipped",
                            f"DOM empty but API saw jobs ({api_jobs_seen}); selector drift likely"
                        ])
                        print("DOM empty but API saw jobs; continuing to next page.")
                        continue

                    if kind == "empty":
                        print("No results indicated by site; treating as last page for this role.")
                        break

                    print("Unknown empty state; trying next page once before stopping role.")
                    continue

                new_cards = [c for c in cards if c.get("jobId") not in seen_job_ids]
                print(f"Found {len(cards)} job cards ({len(new_cards)} new) on page {page_no}. API jobs seen: {api_jobs_seen}")

                if not new_cards:
                    print("No new listings on this page -- treating as the last page for this role.")
                    break

                for card in new_cards:
                    seen_job_ids.add(card.get("jobId"))
                    if applied >= profile.stop_after_n_applications:
                        break

                    ok, reason = passes_filters(card, profile, role)
                    if not ok:
                        log_row([datetime.now(), "naukri", card.get("title"), card.get("company"), "skipped", reason])
                        print(f"Skipped: {card.get('title')} @ {card.get('company')} — {reason}")
                        continue

                    try:
                        page.goto(card["href"])
                        time.sleep(2)

                        stop = page_has_stop_signal(page)
                        if stop:
                            raise StopRun(f"stop signal: {stop}")

                        apply_state = check_apply_button(page)
                        if apply_state == "external":
                            log_row([datetime.now(), "naukri", card.get("title"), card.get("company"), "skipped", "external apply"])
                            print(f"Skipped: {card.get('title')} @ {card.get('company')} — external apply")
                            continue
                        if apply_state == "none":
                            log_row([datetime.now(), "naukri", card.get("title"), card.get("company"), "skipped", "no apply button found"])
                            print(f"Skipped: {card.get('title')} @ {card.get('company')} — no apply button found")
                            continue

                        clicked = click_native_apply(page)
                        if not clicked:
                            log_row([datetime.now(), "naukri", card.get("title"), card.get("company"), "skipped", "apply button click didn't register"])
                            print(f"Skipped: {card.get('title')} @ {card.get('company')} — apply click didn't register")
                            continue

                        time.sleep(2)
                        answer_screening_chat(page, profile, f"{card.get('title')} at {card.get('company')}", human_timeout)
                        time.sleep(1.5)

                        if verify_applied(page):
                            applied += 1
                            log_row([datetime.now(), "naukri", card.get("title"), card.get("company"), "applied", ""])
                            print(f"Applied: {card.get('title')} @ {card.get('company')} ({applied} total)")
                        else:
                            shot_path = _save_debug_screenshot(page, card.get("title"))
                            log_row([datetime.now(), "naukri", card.get("title"), card.get("company"), "uncertain",
                                     f"couldn't confirm submission — screenshot saved to {shot_path}"])
                            print(f"UNCERTAIN: {card.get('title')} @ {card.get('company')} — couldn't confirm apply.")

                    except SkipJob as e:
                        log_row([datetime.now(), "naukri", card.get("title"), card.get("company"), "skipped", str(e)])
                        print(f"Skipped: {card.get('title')} @ {card.get('company')} — {e}")
                        continue

                    except StopRun as e:
                        print(f"STOPPING: {e}")
                        log_row([datetime.now(), "naukri", card.get("title"), card.get("company"), "stopped", str(e)])
                        browser.close()
                        return

                    except Exception as e:
                        log_row([datetime.now(), "naukri", card.get("title"), card.get("company"), "skipped", f"unexpected error: {e}"])
                        print(f"Skipped (unexpected error): {card.get('title')} @ {card.get('company')} — {e}")
                        continue

                    time.sleep(profile.pace_seconds_between_actions)

                if stopped_entirely:
                    break

            if stopped_entirely:
                break

        browser.close()

    print(f"\nDone. {applied} applications submitted this run. See {LOG_FILE} for the full log.")


if __name__ == "__main__":
    run()