"""Type-aware FormSG filler for screenshot-derived attendance profiles.

Field matching is accessibility-based so it survives DOM/class churn:

* radio: question ``group`` -> option accessible name
* checkbox: question ``group`` or unique option accessible name
* text: ``textbox``/label accessible name
* date: day/month/year ``group`` (retained for future profiles)

Profiles are insertion-ordered. Answers are applied iteratively because FormSG
mounts conditional questions only after preceding radio selections. Any expected
answer left unapplied is fatal: the bot must never submit a partial profile.

``--discover`` saves the authenticated form page's HTML (``Page.content``) to
``form-page.html`` for inspection.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .attendance import Answers, AnswerValue

log = logging.getLogger("mdcattendance.formfiller")
FIELD_REVEAL_DELAY_MS = 500
MAX_STALLED_PASSES = 10


def _question_name(title: str) -> re.Pattern[str]:
    """Match an optionally numbered question name in Playwright's JavaScript regex."""
    escaped_title = re.escape(title).replace("/", r"\/")
    return re.compile(rf"^(?:\d+\.\s*)?{escaped_title}(?:\s*\*)?$", re.IGNORECASE)


async def _resolve_radiogroup(page: Page, key: str):
    """Resolve by question name; option values repeat across conditional fields."""
    groups = page.get_by_role("radiogroup", name=_question_name(key))
    if await _count(groups) == 1:
        return groups.first
    return None


async def _count(loc) -> int:
    try:
        return await loc.count()
    except Exception:
        return 0


async def _all_exist(locators: list) -> bool:
    for locator in locators:
        if not await _count(locator):
            return False
    return True

async def _input_by_value(container, input_type: str, value: str):
    inputs = container.locator(f'input[type="{input_type}"]')
    for index in range(await _count(inputs)):
        candidate = inputs.nth(index)
        if await candidate.get_attribute("value") == value:
            return candidate
    return None


async def apply_answer(page: Page, key: str, value: AnswerValue) -> bool:
    """Apply one answer to a currently mounted field. Return whether applied."""
    values = [str(v) for v in (value if isinstance(value, list) else [value])]

    radiogroup = await _resolve_radiogroup(page, key)
    if radiogroup is not None:
        radios = [await _input_by_value(radiogroup, "radio", v) for v in values]
        if all(radio is not None for radio in radios):
            for radio in radios:
                await radio.check(force=True)
            return True

    group = page.get_by_role("group", name=_question_name(key))
    if await _count(group):
        checkboxes = [group.get_by_role("checkbox", name=v, exact=True) for v in values]
        if await _all_exist(checkboxes):
            for checkbox in checkboxes:
                await checkbox.first.check(force=True)
            return True

    # Acknowledgements have duplicate question titles but unique option text.
    checkboxes = [page.get_by_role("checkbox", name=v, exact=True) for v in values]
    if await _all_exist(checkboxes):
        for checkbox in checkboxes:
            await checkbox.first.check(force=True)
        return True
    textbox = page.get_by_role("textbox", name=_question_name(key))
    if await _count(textbox) == 1:
        await textbox.fill(values[0])
        return True
    return bool(await _fill_date_group(page, key, values[0]))


async def _fill_date_group(page: Page, title: str, value: str) -> bool:
    match = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", value)
    if match:
        year, month, day = match.group(1), match.group(2), match.group(3)
    else:
        match = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*", value)
        if not match:
            return False
        day, month, year = match.group(1), match.group(2), match.group(3)

    group = page.get_by_role("group", name=_question_name(title))
    if not await _count(group):
        return False
    selects = group.get_by_role("combobox")
    if await _count(selects) >= 3:
        await selects.nth(0).select_option(day)
        await selects.nth(1).select_option(str(int(month)))
        await selects.nth(2).select_option(year)
        return True
    inputs = group.get_by_role("spinbutton")
    if await _count(inputs) >= 3:
        await inputs.nth(0).fill(day)
        await inputs.nth(1).fill(str(int(month)))
        await inputs.nth(2).fill(year)
        return True
    return False


async def fill_form(page: Page, answers: Answers) -> None:
    remaining = dict(answers)
    stalled_passes = 0
    log.info("filling form: %d answer(s)", len(remaining))
    while remaining and stalled_passes < MAX_STALLED_PASSES:
        progressed = False
        for key, value in list(remaining.items()):
            try:
                applied = await apply_answer(page, key, value)
            except PlaywrightTimeoutError:
                applied = False
            except Exception as error:  # noqa: BLE001
                log.warning("error filling '%s': %s", key, error)
                applied = False
            if applied:
                log.info("filled '%s'", key)
                del remaining[key]
                progressed = True
                await page.wait_for_timeout(FIELD_REVEAL_DELAY_MS)
        if progressed:
            stalled_passes = 0
            continue
        stalled_passes += 1
        if stalled_passes < MAX_STALLED_PASSES:
            await page.wait_for_timeout(FIELD_REVEAL_DELAY_MS)

    if remaining:
        missing = ", ".join(remaining)
        raise RuntimeError(f"refusing to submit; expected answers were not applied: {missing}")


async def discover_form(page: Page, out_path: str = "form-page.html") -> None:
    """Save the authenticated form page's HTML (``Page.content``) to ``out_path``."""
    log.info("saving form HTML -> %s", out_path)
    html = await page.content()
    Path(out_path).write_text(html, encoding="utf-8")
    log.info("wrote form HTML (%d bytes) -> %s", len(html), out_path)
