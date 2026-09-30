"""Attendance-day profiles derived from ``website/snapshots/*.png``.

The dictionaries are insertion-ordered to match the form's reveal flow:
Department -> Status -> conditionally mounted questions -> acknowledgements.
"""

from __future__ import annotations

import re
from enum import StrEnum

from .otp import read_terminal_line

AnswerValue = str | list[str]
Answers = dict[str, AnswerValue]

DEPARTMENT = "Show Production (Including SDE Cap Devt)"
ACK_ACCURACY = "I acknowledge that the above information is accurate to the best of my knowledge."
ACK_MANAGER_APPROVAL = (
    "I acknowledge that the above information has been approved by my Department Manager(s)"
)
ACK_MC_PROCESS = "I understand and acknowledge the above."


class DayType(StrEnum):
    NORMAL = "normal"
    WFH = "wfh"
    MC = "mc"


NORMAL_ANSWERS: Answers = {
    "Department": DEPARTMENT,
    "Status": "Present (Infinite Studios - IS)",
    "Do you have an MA/AL/OIL/WFH?": "NIL",
    "IS Duties": ["Cap Devt Duties"],
    "Remarks (NSC/IS)": "NIL",
    "ack_accuracy": ACK_ACCURACY,
}

WFH_ANSWERS: Answers = {
    "Department": DEPARTMENT,
    "Status": "Work-from-Home (WFH)",
    "Is your status for AM or PM?": "Both (AM & PM)",
    "ack_manager_approval": ACK_MANAGER_APPROVAL,
}

DAY_TYPE_ALIASES = {
    "n": DayType.NORMAL,
    "normal": DayType.NORMAL,
    "w": DayType.WFH,
    "wfh": DayType.WFH,
    "m": DayType.MC,
    "mc": DayType.MC,
}


async def choose_day_type(explicit: str | None, *, timeout: float = 600) -> DayType:
    """Resolve ``--day-type`` or prompt until a supported day type is entered."""
    if explicit is not None:
        return DayType(explicit)

    while True:
        raw = (await read_terminal_line("Day type [normal/wfh/mc]: ", timeout)).strip().lower()
        if day_type := DAY_TYPE_ALIASES.get(raw):
            return day_type
        print("Enter normal, wfh, or mc.")


async def _prompt_required(prompt: str, *, timeout: float = 600) -> str:
    while True:
        value = (await read_terminal_line(prompt, timeout)).strip()
        if value:
            return value
        print("A value is required.")


def mc_answers(clinic: str, timing: str) -> Answers:
    return {
        "Department": DEPARTMENT,
        "Status": "Medical Certificate (MC)",
        "MC Visit Status": "Pre-booked Appointment",
        "Clinic / Hospital / Medical Centre Name": clinic,
        "Appointment Timing": timing,
        "ack_accuracy": ACK_ACCURACY,
        "ack_mc_process": ACK_MC_PROCESS,
    }


def validate_answers(answers: Answers) -> None:
    """Accept only complete supported profiles, without rewriting their answers."""
    if answers in (NORMAL_ANSWERS, WFH_ANSWERS):
        return
    clinic = answers.get("Clinic / Hospital / Medical Centre Name")
    timing = answers.get("Appointment Timing")
    if not isinstance(clinic, str) or not clinic.strip():
        raise ValueError("a supported complete attendance profile is required")
    if not isinstance(timing, str) or not re.fullmatch(
        r"(?:[01][0-9]|2[0-3])[0-5][0-9](?:hrs)?", timing
    ):
        raise ValueError("MC appointment timing must be HHMM, optionally followed by hrs")
    if answers != mc_answers(clinic, timing):
        raise ValueError("MC answers must match the complete supported profile")


async def build_answers(day_type: DayType, *, timeout: float = 600) -> Answers:
    """Build the exact ordered answers for ``day_type``.

    Clinic and appointment timing vary per MC visit, so they are always
    collected interactively instead of being stored in a static profile.
    """
    if day_type is DayType.NORMAL:
        return dict(NORMAL_ANSWERS)
    if day_type is DayType.WFH:
        return dict(WFH_ANSWERS)

    clinic = await _prompt_required("Clinic / Hospital / Medical Centre Name: ", timeout=timeout)
    timing = await _prompt_required("Appointment Timing (24hr, e.g. 0930hrs): ", timeout=timeout)
    return mc_answers(clinic, timing)
