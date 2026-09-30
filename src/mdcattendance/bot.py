"""Playwright automation of the Singpass -> FormSG attendance flow.

Selector provenance
-------------------
Selectors marked **VERIFIED** were captured by driving the live site with the
browser tool against the real DOM/ARIA tree:

  * form.gov.sg landing page -> button "Log in with Singpass"
  * login.id.singpass.gov.sg -> button "Use password" (visible text; the button's
    aria-label is "Log in using password authentication", so role/name matching
    targets the accessible name, not the visible text)
  * Singpass ID field  -> input#username, aria-label "Singpass ID", autocomplete "username"
  * Password field     -> input#password, type=password, aria-label "Password"
  * Submit             -> button[type=submit], visible text "Log in"

Selectors marked **INFERRED** (OTP entry, Cancel/I Agree consent, form submit)
sit behind the Singpass auth boundary and could not be captured without live
credentials. They use resilient accessible-role/text heuristics and are
confirmed via the ``--discover`` run or a real-credentialed run.

OTP source
----------
The 2FA OTP comes from an ``OtpProvider`` (see ``otp.py``). By default the
operator types it in the terminal (``CliOtpProvider``); the HTTP bridge is used
with ``--http-otp``. The bot is agnostic to the source.
"""

from __future__ import annotations

import logging
import re
import time

from playwright.async_api import (
    Page,
    async_playwright,
    expect,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .config import Config
from .formfiller import discover_form, fill_form
from .otp import OtpProvider

log = logging.getLogger("mdcattendance.bot")

SINGPASS_HOST = "login.id.singpass.gov.sg"
FORMSG_HOST = "form.gov.sg"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


async def _wait_url_contains(page: Page, fragment: str, *, timeout_ms: int) -> None:
    await page.wait_for_url(lambda url: fragment in url, timeout=timeout_ms)


async def _safe_count(loc) -> int:
    try:
        return await loc.count()
    except Exception:
        return 0


async def _safe_visible(loc, *, timeout_ms: int = 4000) -> bool:
    try:
        await expect(loc.first).to_be_visible(timeout=timeout_ms)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# public entry
# --------------------------------------------------------------------------- #


async def run_flow(cfg: Config, otp: OtpProvider, answers: dict) -> None:
    async with async_playwright() as p:
        launch_kwargs: dict = {
            "headless": cfg.headless,
            "slow_mo": cfg.slow_mo,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if cfg.chromium_executable_path:
            launch_kwargs["executable_path"] = cfg.chromium_executable_path
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-SG")
        page = await context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout_ms)
        try:
            await _open_form(page, cfg.form_url)
            if cfg.preflight:
                await _preflight(page, cfg)
                return
            await _login_with_singpass(page, cfg, otp)
            await _consent_agree(page, cfg)
            await _back_to_form(page, cfg)
            if cfg.discover:
                await discover_form(page)
                log.info("discover complete (nothing submitted)")
                return
            await fill_form(page, answers)
            if cfg.dry_run:
                log.info(
                    "dry run complete; form filled but not submitted; closing in 30 seconds"
                )
                await page.wait_for_timeout(30_000)
                return
            await _submit_form(page)
            await _confirm_end_page(page)
            log.info("attendance submitted successfully")
        finally:
            await context.close()
            await browser.close()


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #


async def _open_form(page: Page, form_url: str) -> None:
    log.info("opening form: %s", form_url)
    await page.goto(form_url, wait_until="domcontentloaded")
    # VERIFIED: button accessible name "Log in with Singpass" (no aria-label override)
    btn = page.get_by_role("button", name="Log in with Singpass")
    await expect(btn).to_be_visible(timeout=20000)
    log.info("form page ready: 'Log in with Singpass' visible")


async def _goto_singpass(page: Page, timeout_ms: int) -> None:
    # VERIFIED: triggers redirect to login.id.singpass.gov.sg
    await page.get_by_role("button", name="Log in with Singpass").click()
    await _wait_url_contains(page, SINGPASS_HOST, timeout_ms=timeout_ms)
    log.info("reached Singpass login page")


async def _click_use_password(page: Page) -> None:
    # VERIFIED: visible text "Use password"; aria-label is "Log in using password
    # authentication", so match by subtree text, not role/name.
    btn = page.locator("button", has_text="Use password")
    await expect(btn.first).to_be_visible(timeout=15000)
    await btn.first.click()
    log.info("clicked 'Use password'")


async def _preflight(page: Page, cfg: Config) -> None:
    """Verify selectors up to the credential form without submitting anything."""
    await _goto_singpass(page, cfg.navigation_timeout_ms)
    await _click_use_password(page)
    # VERIFIED: Singpass ID / Password / submit locators
    sid = page.get_by_label("Singpass ID", exact=True)
    pw = page.get_by_label("Password", exact=True)
    submit = page.locator('button[type="submit"]')
    await expect(sid).to_be_visible(timeout=15000)
    await expect(pw).to_be_visible(timeout=15000)
    await expect(submit).to_be_visible(timeout=15000)
    log.info(
        "PREFLIGHT OK: Singpass ID, Password, and 'Log in' submit all present. "
        "No credentials submitted."
    )


async def _login_with_singpass(page: Page, cfg: Config, otp: OtpProvider) -> None:
    await _goto_singpass(page, cfg.navigation_timeout_ms)
    await _click_use_password(page)
    log.info("entering Singpass credentials")
    # VERIFIED: aria-label-based locators
    await page.get_by_label("Singpass ID", exact=True).fill(cfg.singpass_id)
    await page.get_by_label("Password", exact=True).fill(cfg.singpass_password)
    # VERIFIED: unique submit button on the password page
    await page.locator('button[type="submit"]').click()
    log.info("credentials submitted; waiting for OTP page")
    await _wait_for_otp_page(page, timeout_ms=60000)
    log.info("OTP page reached; waiting for OTP...")
    code = await otp.wait_for_otp(timeout=cfg.otp_timeout)
    log.info("OTP received (%d chars); entering it", len(code))
    await _enter_otp(page, code)
    await _submit_otp(page)


async def _wait_for_otp_page(page: Page, *, timeout_ms: int) -> None:
    # INFERRED: OTP entry UI. Singpass 2FA renders either a one-time-code input
    # or a row of single-digit inputs. Wait for either, or for OTP-ish text.
    selectors = [
        'input[autocomplete="one-time-code"]',
        'input[maxlength="1"]',
        'input[inputmode="numeric"]',
    ]
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in selectors:
            if await _safe_visible(page.locator(sel).first, timeout_ms=1000):
                return
        if await _safe_count(page.get_by_text(re.compile("one-time|otp|verification", re.I))):
            return
        await page.wait_for_timeout(500)
    raise PlaywrightTimeoutError("OTP entry UI did not appear in time")


async def _enter_otp(page: Page, otp: str) -> None:
    digits = re.sub(r"\D", "", otp)
    # INFERRED: N single-char inputs (one per digit)
    singles = page.locator('input[maxlength="1"]')
    if await _safe_count(singles) >= len(digits):
        for i, ch in enumerate(digits):
            await singles.nth(i).fill(ch)
        await page.wait_for_timeout(300)
        return
    # INFERRED: a single OTP input
    for sel in ('input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]'):
        single = page.locator(sel).first
        if await _safe_count(single):
            await single.fill(digits)
            return
    tb = page.get_by_role("textbox").first
    if await _safe_count(tb):
        await tb.fill(digits)
        return
    await page.keyboard.type(digits)


async def _submit_otp(page: Page) -> None:
    # INFERRED: a verify/submit/continue button, else Enter.
    for name in ("Verify", "Submit", "Continue", "Next", "Log in", "Confirm"):
        btn = page.get_by_role("button", name=name)
        if await _safe_count(btn):
            try:
                await btn.first.click(timeout=3000)
                log.info("submitted OTP via '%s' button", name)
                return
            except Exception:
                continue
    sub = page.locator('button[type="submit"]')
    if await _safe_count(sub):
        await sub.first.click()
        return
    await page.keyboard.press("Enter")
    log.info("submitted OTP via Enter")


async def _consent_agree(page: Page, cfg: Config) -> None:
    # Singpass can reuse prior MyInfo consent and redirect straight back to FormSG.
    log.info("waiting for consent (Cancel / I Agree) page or authenticated form")
    agree = page.locator("button", has_text="I Agree")
    form = page.locator('form[novalidate] div[role="radiogroup"]').first
    await expect(agree.or_(form).first).to_be_visible(
        timeout=cfg.navigation_timeout_ms
    )
    if await form.is_visible():
        log.info("consent already satisfied; redirected directly to authenticated form")
        return
    await agree.first.click()
    log.info("clicked 'I Agree' on consent page")


async def _back_to_form(page: Page, cfg: Config) -> None:
    await _wait_url_contains(page, FORMSG_HOST, timeout_ms=cfg.navigation_timeout_ms)
    # networkidle never reliably settles on FormSG (reCAPTCHA/analytics keep
    # sockets open), so wait for the SPA to mount the form itself instead.
    await page.wait_for_selector(
        'form[novalidate] div[role="radiogroup"]',
        state="visible",
        timeout=cfg.navigation_timeout_ms,
    )
    # Settle so conditional questions finish mounting before fill/discover.
    await page.wait_for_timeout(cfg.form_settle_ms)
    log.info("back on FormSG form page (authenticated)")


async def _submit_form(page: Page) -> None:
    # INFERRED: FormSG submit button.
    for name in ("Submit", "Submit form", "Proceed", "Confirm"):
        btn = page.get_by_role("button", name=name)
        if await _safe_count(btn):
            await btn.first.click()
            log.info("clicked '%s'", name)
            return
    sub = page.locator('button[type="submit"]')
    if await _safe_count(sub):
        await sub.first.click()
        log.info("clicked submit button")
        return
    raise PlaywrightTimeoutError("could not find the form submit button")


async def _confirm_end_page(page: Page) -> None:
    # VERIFIED (from schema endPage): "Thank you for filling out the form."
    await expect(
        page.get_by_text("Thank you for filling out the form.")
    ).to_be_visible(timeout=30000)
