"""Sandboxed Singpass/FormSG execution with fail-closed submission.

Password selectors and FormSG submit/confirmation names come from captured DOM.
OTP input labels include user-reported DOM; other controls remain inferred.
Accept only explicit OTP controls, never arbitrary textboxes or keyboard fallback.
Discovery is explicit and restricted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import (
    Page,
    async_playwright,
    expect,
)

from .attendance import Answers, validate_answers
from .config import Config
from .formfiller import discover_form, fill_form, verify_form
from .otp import OtpProvider, validate_otp

log = logging.getLogger("mdcattendance.bot")

SINGPASS_HOST = "login.id.singpass.gov.sg"
FORMSG_HOST = "form.gov.sg"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


async def _wait_url_contains(page: Page, fragment: str, *, timeout_ms: int) -> None:
    await page.wait_for_url(lambda url: urlparse(str(url)).hostname == fragment, timeout=timeout_ms)


# --------------------------------------------------------------------------- #
# public entry
# --------------------------------------------------------------------------- #


async def run_flow(
    cfg: Config,
    otp: OtpProvider,
    answers: Answers,
    *,
    before_submit: Callable[[], Awaitable[None]] | None = None,
) -> str:
    if not cfg.preflight and not cfg.discover:
        validate_answers(answers)
        if not cfg.dry_run and before_submit is None:
            raise RuntimeError("submission requires a durable before_submit callback")
    _purge_diagnostics(cfg)
    async with async_playwright() as p:
        browser = context = None
        try:
            launch_kwargs: dict = {
                "headless": cfg.headless,
                "slow_mo": cfg.slow_mo,
                "chromium_sandbox": True,
            }
            if cfg.chromium_executable_path:
                launch_kwargs["executable_path"] = cfg.chromium_executable_path
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="en-SG",
                timezone_id="Asia/Singapore",
            )
            page = await context.new_page()
            page.set_default_navigation_timeout(cfg.navigation_timeout_ms)
            await _open_form(page, cfg.form_url)
            if cfg.preflight:
                await _preflight(page, cfg)
                return "preflight"
            await _login_with_singpass(page, cfg, otp)
            await _consent_agree(page, cfg)
            await _back_to_form(page, cfg)
            if cfg.discover:
                await discover_form(page, _diagnostic_path(cfg))
                return "discovered"
            await fill_form(page, answers)
            await verify_form(page, answers)
            if cfg.dry_run:
                return "dry_run"
            if before_submit is None:
                raise RuntimeError("submission requires a durable before_submit callback")
            await before_submit()
            await _submit_form(page)
            await _confirm_end_page(page)
            log.info("attendance confirmation observed")
            return "confirmed"
        finally:

            async def close_resources():
                try:
                    if context is not None:
                        await context.close()
                finally:
                    if browser is not None:
                        await browser.close()

            cleanup = asyncio.create_task(close_resources())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError


def _diagnostic_path(cfg: Config) -> str:
    directory = Path(cfg.state_dir) / "diagnostics"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeError("diagnostics directory must not be a symlink")
    os.chmod(directory, 0o700)
    _purge_diagnostics(cfg)
    return str(directory / f"form-{uuid4().hex}.html")


def _purge_diagnostics(cfg: Config) -> None:
    directory = Path(cfg.state_dir) / "diagnostics"
    if directory.is_symlink():
        raise RuntimeError("diagnostics directory must not be a symlink")
    if not directory.exists():
        return
    cutoff = time.time() - cfg.diagnostic_ttl_hours * 3600
    for path in directory.glob("form-*.html"):
        if path.is_symlink() or path.stat().st_mtime < cutoff:
            path.unlink()


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #


async def _open_form(page: Page, form_url: str) -> None:
    log.info("opening attendance form")
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
    # VERIFIED accessible name on the password authentication switch.
    btn = page.get_by_role("button", name="Log in using password authentication", exact=True)
    await expect(btn).to_be_visible(timeout=15000)
    await btn.click()
    log.info("clicked 'Use password'")


async def _preflight(page: Page, cfg: Config) -> None:
    """Verify selectors up to the credential form without submitting anything."""
    await _goto_singpass(page, cfg.navigation_timeout_ms)
    await _click_use_password(page)
    # VERIFIED: Singpass ID / Password / submit locators
    sid = page.get_by_label("Singpass ID", exact=True)
    pw = page.get_by_label("Password", exact=True)
    submit = page.get_by_role("button", name="Submit password for Singpass login", exact=True)
    await expect(sid).to_be_visible(timeout=15000)
    await expect(pw).to_be_visible(timeout=15000)
    await expect(submit).to_be_visible(timeout=15000)
    log.info(
        "PREFLIGHT OK: Singpass ID, Password, and 'Log in' submit all present. No credentials submitted."
    )


async def _login_with_singpass(page: Page, cfg: Config, otp: OtpProvider) -> None:
    await _goto_singpass(page, cfg.navigation_timeout_ms)
    await _click_use_password(page)
    log.info("entering Singpass credentials")
    # VERIFIED: aria-label-based locators
    await page.get_by_label("Singpass ID", exact=True).fill(cfg.singpass_id)
    await page.get_by_label("Password", exact=True).fill(cfg.singpass_password)
    # VERIFIED: unique submit button on the password page
    await page.get_by_role("button", name="Submit password for Singpass login", exact=True).click()
    log.info("credentials submitted; waiting for OTP page")
    await _wait_for_otp_page(page, timeout_ms=60000)
    log.info("OTP page ready; waiting for OTP delivery")
    code = validate_otp(await otp.wait_for_otp(timeout=cfg.otp_timeout))
    log.info("OTP received; entering it")
    await _enter_otp(page, code)
    await _submit_otp(page)


async def _wait_for_otp_page(page: Page, *, timeout_ms: int) -> None:
    # USER-REPORTED: "Enter 6-digit OTP code"; INFERRED: autocomplete or six digits.
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if urlparse(page.url).hostname != SINGPASS_HOST:
            raise RuntimeError("unexpected authentication page while waiting for OTP")
        single = page.locator('input[autocomplete="one-time-code"]').or_(
            page.get_by_label("Enter 6-digit OTP code", exact=True)
        )
        singles = page.locator('input[maxlength="1"][inputmode="numeric"]')
        if await single.count() == 1 and await single.is_visible():
            return
        if await singles.count() == 6 and all([await singles.nth(i).is_visible() for i in range(6)]):
            return
        await page.wait_for_timeout(250)
    raise RuntimeError("expected OTP controls did not appear; authentication page unsupported")


async def _enter_otp(page: Page, otp: str) -> None:
    digits = validate_otp(otp)
    if urlparse(page.url).hostname != SINGPASS_HOST:
        raise RuntimeError("unexpected authentication page")
    single = page.locator('input[autocomplete="one-time-code"]').or_(
        page.get_by_label("Enter 6-digit OTP code", exact=True)
    )
    if await single.count() == 1 and await single.is_visible():
        await single.fill(digits)
        return
    singles = page.locator('input[maxlength="1"][inputmode="numeric"]')
    if await singles.count() == 6:
        for i, ch in enumerate(digits):
            await singles.nth(i).fill(ch)
        return
    raise RuntimeError("expected OTP controls missing; refusing keyboard fallback")


async def _submit_otp(page: Page) -> None:
    # INFERRED names, exact and unique; unsupported authentication fails closed.
    if urlparse(page.url).hostname != SINGPASS_HOST:
        raise RuntimeError("unexpected authentication page")
    candidates = []
    for name in ("Verify", "Submit", "Continue", "Next", "Log in", "Confirm"):
        button = page.get_by_role("button", name=name, exact=True)
        if await button.count() == 1 and await button.is_visible():
            candidates.append(button)
    if len(candidates) != 1:
        raise RuntimeError("expected unique OTP submission control missing")
    await candidates[0].click()


async def _consent_agree(page: Page, cfg: Config) -> None:
    # Singpass can reuse prior MyInfo consent and redirect straight back to FormSG.
    log.info("waiting for consent (Cancel / I Agree) page or authenticated form")
    agree = page.locator("button", has_text="I Agree")
    form = page.locator('form[novalidate] div[role="radiogroup"]').first
    await expect(agree.or_(form).first).to_be_visible(timeout=cfg.navigation_timeout_ms)
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
    # VERIFIED: form-aria-snapshot.yaml accessible name.
    button = page.get_by_role("button", name="End of form. Submit now", exact=True)
    await expect(button).to_be_visible()
    await expect(button).to_be_enabled()
    await button.click()


async def _confirm_end_page(page: Page) -> None:
    # VERIFIED (from schema endPage): "Thank you for filling out the form."
    await expect(page.get_by_text("Thank you for filling out the form.")).to_be_visible(timeout=30000)
