"""Attendance-day profiles derived from ``website/snapshots/*.png``.

The dictionaries are insertion-ordered to match the form's reveal flow:
Department -> Status -> conditionally mounted questions -> acknowledgements.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum

AnswerValue = str | list[str]
Answers = dict[str, AnswerValue]

DEPARTMENT = "Show Production (Including SDE Cap Devt)"
ACK_ACCURACY = (
    "I acknowledge that the above information is accurate to the best of my knowledge."
)
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


async def choose_day_type(explicit: str | None) -> DayType:
    """Resolve ``--day-type`` or prompt until a supported day type is entered."""
    if explicit is not None:
        return DayType(explicit)

    while True:
        raw = (await asyncio.to_thread(input, "Day type [normal/wfh/mc]: ")).strip().lower()
        if day_type := DAY_TYPE_ALIASES.get(raw):
            return day_type
        print("Enter normal, wfh, or mc.")


async def _prompt_required(prompt: str) -> str:
    while True:
        value = (await asyncio.to_thread(input, prompt)).strip()
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


async def build_answers(day_type: DayType) -> Answers:
    """Build the exact ordered answers for ``day_type``.

    Clinic and appointment timing vary per MC visit, so they are always
    collected interactively instead of being stored in a static profile.
    """
    if day_type is DayType.NORMAL:
        return dict(NORMAL_ANSWERS)
    if day_type is DayType.WFH:
        return dict(WFH_ANSWERS)

    clinic = await _prompt_required("Clinic / Hospital / Medical Centre Name: ")
    timing = await _prompt_required("Appointment Timing (24hr, e.g. 0930hrs): ")
    return mc_answers(clinic, timing)
