# Work-from-Home (WFH)

Matches `WFH_ANSWERS` in `src/mdcattendance/attendance.py`
(`--day-type wfh`).

| Order | Question | Answer |
| --- | --- | --- |
| 1 | Name | `<prefilled via Singpass/MyInfo>` |
| 2 | Department | Show Production (Including SDE Cap Devt) |
| 3 | Status | Work-from-Home (WFH) |
| 4 | Is your status for AM or PM? | Both (AM & PM) |
| 5 | Acknowledgement of Status | I acknowledge that the above information has been approved by my Department Manager(s) |

## Is your status for AM or PM?

The stored profile always selects `Both (AM & PM)`, which reveals no follow-up
question.

| Selection | Revealed question | Answer |
| --- | --- | --- |
| Both (AM & PM) | — none — | — |
| AM | What is your PM Status | `<PM arrangement>` |
| PM | What is your AM Status | `<AM arrangement>` |

Notes:

- WFH uses the manager-approval acknowledgement, not the accuracy one used by
  the Present (IS) and MC profiles.
