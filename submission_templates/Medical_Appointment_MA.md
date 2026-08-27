# Medical Appointment (MA)

No bot profile exists for this status yet — values marked `TBC` need
confirmation against the live form.

| Order | Question | Answer |
| --- | --- | --- |
| 1 | Name | `<prefilled via Singpass/MyInfo>` |
| 2 | Department | Show Production (Including SDE Cap Devt) |
| 3 | Status | Medical Appointment (MA) |
| 4 | Is your status for AM or PM? | see below |
| 5 | Medical Appointment (MA) Time | `<e.g. 0930hrs>` |

## Is your status for AM or PM?

Only the `Both (AM & PM)` option value is verified (from the WFH profile);
`AM` / `PM` labels are assumed.

| Selection | Revealed question | Answer |
| --- | --- | --- |
| Both (AM & PM) | — none — | — |
| AM | What is your PM Status | `<PM arrangement>` |
| PM | What is your AM Status | `<AM arrangement>` |
