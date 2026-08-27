# Medical Certificate (MC)

Matches the interactive MC profile in `build_answers`
(`src/mdcattendance/attendance.py`). Clinic name and appointment timing are
prompted per run, never stored.

| Order | Question | Answer |
| --- | --- | --- |
| 1 | Name | `<prefilled via Singpass/MyInfo>` |
| 2 | Department | Show Production (Including SDE Cap Devt) |
| 3 | Status | Medical Certificate (MC) |
| 4 | MC Visit Status | see branch below |

## Branch: Pre-booked Appointment (bot default)

Note: `attendance.py` stores `Pre-booked Appointment`; FORM.md says
`Pre-book Appointment`. Confirm exact wording on the live form before relying
on either.

| Order | Question | Answer |
| --- | --- | --- |
| 5 | Clinic / Hospital / Medical Centre Name | prompted per run |
| 6 | Appointment Timing | prompted per run (`<24hr, e.g. 0930hrs>`) |
| 7 | Acknowledgement of Pre-Booking Status | `TBC` (checkbox text not yet captured) |
| 8 | Acknowledgement of MC Process | I understand and acknowledge the above. |

## Branch: Walk-In Appointment

| Order | Question | Answer |
| --- | --- | --- |
| 5 | Clinic / Hospital / Medical Centre Name | prompted per run |
| 6 | Appointment Timing (Indicate estimated/fixed appointment time) | prompted per run |
| 7 | Acknowledgement of Walk-In Status | `TBC` (checkbox text not yet captured) |
| 8 | Acknowledgement of MC Process | I understand and acknowledge the above. |

Notes:

- The bot also answers `Acknowledgement of Status` with "I acknowledge that the
  above information is accurate to the best of my knowledge." for MC runs.
