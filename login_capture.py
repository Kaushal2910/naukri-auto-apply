"""
Run this once per site (Naukri, LinkedIn) before running the apply scripts.

It opens a real, visible Chromium window. YOU log in by hand — type your
password, complete 2FA, solve any CAPTCHA yourself. Once you're on the
logged-in homepage, come back to this terminal and press Enter. The script
then saves your session (cookies + local storage) to a local file so the
apply scripts can reuse it without ever seeing your password.

Usage:
    python login_capture.py naukri
    python login_capture.py linkedin
"""
import os
import sys
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

SITES = {
    "naukri": "https://www.naukri.com/nlogin/login",
    "linkedin": "https://www.linkedin.com/login",
}

def main():
    # --- Argument parsing ---
    use_auto_login = "--auto-login" in sys.argv
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]

    if not positional:
        print(f"Usage: python login_capture.py [{'|'.join(SITES)}] [--auto-login]")
        sys.exit(1)

    site = positional[0]
    if site not in SITES:
        print(f"Invalid site '{site}'. Choose from: {', '.join(SITES.keys())}")
        sys.exit(1)

    url = SITES[site]
    out_path = f"session_{site}.json"

    with sync_playwright() as p:
        # Headless in CI (auto-login mode), visible window otherwise
        headless = use_auto_login
        browser = p.chromium.launch(headless=headless, args=[] if not headless else ["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)

        if use_auto_login:
            email = os.environ.get("EMAIL")
            password = os.environ.get("PASSWORD")
            if not email or not password:
                raise RuntimeError(
                    "Auto-login failed: EMAIL or PASSWORD environment variables not set. "
                    "Check that secrets NAUKRI_EMAIL and NAUKRI_PASSWORD are defined in the GitHub workflow."
                )

            # ---- Auto-fill login form ----
            page.screenshot(path="login_page_debug.png")  # Debug: save login page
            print("Login page screenshot saved as login_page_debug.png")
            
            # Check if we're actually on login page
            current_url = page.url
            print(f"Current URL: {current_url}")
            
            # Use aria-label selectors matching Naukri's actual form
            page.fill('input[aria-label="Email ID / Username"]', email)
            page.fill('input[aria-label="Password"]', password)
            page.click('button[type="submit"]')

            # Wait for login to complete — either redirect to homepage or a CAPTCHA block
            try:
                page.wait_for_url("https://www.naukri.com/*", timeout=15000)
                print("Login successful (redirected).")
            except PWTimeout:
                # Redirect didn't happen within timeout — check if we're still on login page
                current_url = page.url
                if "nlogin" in current_url:
                    raise RuntimeError(
                        "Auto-login timed out — still on login page. "
                        "Check credentials or solve CAPTCHA manually."
                    )
                else:
                    print(f"Login may have succeeded but URL is unexpected: {current_url}")

            # Extra safety: confirm we are logged in by navigating to homepage
            try:
                page.goto("https://www.naukri.com/", timeout=10000)
                page_content = page.content().lower()
                if "logout" in page_content:
                    print("Confirmed logged in (logout link found).")
                else:
                    print("Warning: logout link not found — session may not be fully established.")
            except Exception as e:
                print(f"Warning: could not navigate to homepage to confirm login: {e}")

            # Save session
            context.storage_state(path=out_path)
            print(f"Session saved to {out_path}.")

        else:
            print(f"\nA browser window is open at {url}")
            print("Log in by hand: password, 2FA, any CAPTCHA — all of it.")
            input("Once you're on the logged-in homepage, press Enter here to save the session... ")
            context.storage_state(path=out_path)
            print(f"Session saved to {out_path}. Keep this file private — it's equivalent to being logged in.")

        browser.close()


if __name__ == "__main__":
    main()
