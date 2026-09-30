"""Type-aware FormSG filler for screenshot-derived attendance profiles.

Field matching is accessibility-based so it survives DOM/class churn:

* radio: question ``group`` -> option accessible name
* checkbox: question ``group`` or unique option accessible name
* text: ``textbox``/label accessible name

Profiles are insertion-ordered. Answers are applied iteratively because FormSG
mounts conditional questions only after preceding radio selections. Any expected
answer left unapplied is fatal: the bot must never submit a partial profile.

Explicit discovery saves restricted HTML to the state diagnostics directory.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .attendance import Answers, AnswerValue

log = logging.getLogger("mdcattendance.formfiller")
FIELD_REVEAL_DELAY_MS = 500
MAX_STALLED_PASSES = 10


def _question_name(title: str) -> re.Pattern[str]:
    """Match an optionally numbered question name in Playwright's JavaScript regex."""
    escaped_title = re.escape(title).replace("/", r"\/")
    return re.compile(rf"^(?:\d+\.\s*)?{escaped_title}(?:\s*\*)?$", re.IGNORECASE)


async def _resolve_radiogroup(page: Page, key: str) -> Locator | None:
    """Resolve by question name; option values repeat across conditional fields."""
    groups = page.get_by_role("radiogroup", name=_question_name(key))
    if await _count(groups) == 1:
        return groups.first
    return None


async def _count(loc: Locator) -> int:
    try:
        return await loc.count()
    except Exception:
        return 0


async def _all_exist(locators: list[Locator]) -> bool:
    for locator in locators:
        if await _count(locator) != 1:
            return False
    return True


async def _input_by_value(container: Locator, input_type: str, value: str) -> Locator | None:
    inputs = container.locator(f'input[type="{input_type}"]')
    for index in range(await _count(inputs)):
        candidate = inputs.nth(index)
        if await candidate.get_attribute("value") == value:
            return candidate
    return None


async def _resolve_answer(
    page: Page, key: str, value: AnswerValue
) -> tuple[str, list[Locator], Locator | None] | None:
    values = value if isinstance(value, list) else [value]
    radiogroup = await _resolve_radiogroup(page, key)
    if radiogroup is not None:
        if len(values) != 1:
            return None
        radios: list[Locator] = []
        for option in values:
            radio = await _input_by_value(radiogroup, "radio", option)
            if radio is None:
                return None
            radios.append(radio)
        return "radio", radios, radiogroup.locator('input[type="radio"]')
    group = page.get_by_role("group", name=_question_name(key))
    if await _count(group) == 1:
        checkboxes = [group.get_by_role("checkbox", name=v, exact=True) for v in values]
        if await _all_exist(checkboxes):
            return "checkbox", checkboxes, group.get_by_role("checkbox")
    # Acknowledgements have duplicate question titles but unique option text.
    checkboxes = [page.get_by_role("checkbox", name=v, exact=True) for v in values]
    if await _all_exist(checkboxes):
        return "checkbox", checkboxes, None
    textbox = page.get_by_role("textbox", name=_question_name(key))
    if isinstance(value, str) and await _count(textbox) == 1:
        return "text", [textbox], None
    return None


async def apply_answer(page: Page, key: str, value: AnswerValue) -> bool:
    """Apply one answer using the same resolution as strict read-back."""
    resolved = await _resolve_answer(page, key, value)
    if resolved is None:
        return False
    kind, controls, _ = resolved
    for control in controls:
        if kind == "text":
            if not isinstance(value, str):
                return False
            await control.fill(value)
        else:
            await control.check(force=True)
    return True


async def verify_form(page: Page, answers: Answers) -> None:
    """Read, never repair, every expected answer; fail closed on any mismatch."""
    for key, value in answers.items():
        resolved = await _resolve_answer(page, key, value)
        if resolved is None:
            raise RuntimeError(f"refusing to submit; expected field is missing: {key}")
        kind, controls, siblings = resolved
        if kind == "text":
            matches = await controls[0].input_value() == value
        else:
            matches = all([await control.is_checked() for control in controls])
            if siblings is not None:
                checked = sum(
                    [await siblings.nth(index).is_checked() for index in range(await siblings.count())]
                )
                matches = matches and checked == len(controls)
        if not matches:
            raise RuntimeError(f"refusing to submit; answer changed: {key}")


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
            except Exception:  # noqa: BLE001
                log.warning("unable to fill expected field '%s'", key)
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


async def discover_form(page: Page, out_path: str) -> None:
    """Explicitly save authenticated HTML without following symlinks, mode 0600."""
    html = await page.content()
    path = Path(out_path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(html)
    log.info("discovery saved to restricted state diagnostics")
