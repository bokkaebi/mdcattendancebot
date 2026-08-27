# Form Layout

## Overview

The form is a conditional form flow. The initial section contains three mandatory questions:

1. Name
2. Department
3. Status

The selection made in **Status** determines the additional questions displayed.

---

## Initial Form

```
Form Start
│
├── Name
│
├── Department
│
└── Status
    │
    ├── Present (Nee Soon Camp - NSC)
    │
    ├── Present (Infinite Studios - IS)
    │
    ├── Deployment (Actual Show Day)
    │
    ├── Other Deployments (Not NSC or IS)
    │
    ├── Medical Appointment (MA)
    │
    ├── Medical Certificate (MC)
    │
    ├── Hospitalisation Leave (HL)
    │
    ├── Annual Leave
    │
    ├── Overseas Leave
    │
    ├── OIL/PHIL
    │
    ├── Birthday Off
    │
    └── Work-from-Home (WFH)
```

---

# Conditional Form Flows

## 1. Present (Nee Soon Camp - NSC)

```
Status: Present (Nee Soon Camp - NSC)

├── Do you have an MA/AL/OIL/WFH?
├── Remarks (NSC/IS)
└── Acknowledgement of Status
```

---

## 2. Present (Infinite Studios - IS)

```
Status: Present (Infinite Studios - IS)

├── Do you have an MA/AL/OIL/WFH?
├── IS Duties
├── Remarks (NSC/IS)
└── Acknowledgement of Status
```

---

## 3. Deployment (Actual Show Day)

```
Status: Deployment (Actual Show Day)

├── Roles
├── Remarks (Location & Reporting Time)
├── Date of Deployment
└── Acknowledgement of Status
```

---

## 4. Other Deployments (Not NSC or IS)

```
Status: Other Deployments (Not NSC or IS)

├── Remarks (Event Name)
├── Remarks (Location & Reporting Time)
├── Date of Deployment
└── Acknowledgement of Status
```

---

## 5. Medical Appointment (MA)

```
Status: Medical Appointment (MA)

├── Is your status for AM or PM?
│
├── AM
│   └── What is your PM Status
│
└── PM
    └── What is your AM Status

└── Medical Appointment (MA) Time
```

---

## 6. Medical Certificate (MC)

```
Status: Medical Certificate (MC)

└── MC Visit Status
    │
    ├── Walk-In Appointment
    │   │
    │   ├── Clinic / Hospital / Medical Centre Name
    │   ├── Appointment Timing (Indicate estimated/fixed appointment time)
    │   ├── Acknowledgement of Walk-In Status
    │   └── Acknowledgement of MC Process
    │
    └── Pre-book Appointment
        │
        ├── Clinic / Hospital / Medical Centre Name
        ├── Appointment Timing
        ├── Acknowledgement of Pre-Booking Status
        └── Acknowledgement of MC Process
```

---

## 7. Hospitalisation Leave (HL)

```
Status: Hospitalisation Leave (HL)

├── Start Date (HL)
├── End Date (HL)
└── Acknowledgement of HL
```

---

## 8. Annual Leave

```
Status: Annual Leave

├── Is your status for AM or PM?
│
├── AM
│   └── What is your PM Status
│
└── PM
    └── What is your AM Status

├── Start Date (AL/OL)
├── End Date (AL/OL)
└── Acknowledgement of Status
```

---

## 9. Overseas Leave

```
Status: Overseas Leave

├── Is your status for AM or PM?
│
├── AM
│   └── What is your PM Status
│
└── PM
    └── What is your AM Status

├── Start Date (AL/OL)
├── End Date (AL/OL)
└── Acknowledgement of Status
```

---

## 10. OIL/PHIL

```
Status: OIL/PHIL

├── Is your status for AM or PM?
│
├── AM
│   └── What is your PM Status
│
└── PM
    └── What is your AM Status

├── Remarks (OIL/PHIL)
├── Start Date (OIL/PHIL/Birthday Off)
├── End Date (OIL/PHIL/Birthday Off)
└── Acknowledgement of Status
```

---

## 11. Birthday Off

```
Status: Birthday Off

├── Date of Birth
├── Start Date (OIL/PHIL/Birthday Off)
├── End Date (OIL/PHIL/Birthday Off)
└── Acknowledgement of Status
```

---

## 12. Work-from-Home (WFH)

```
Status: Work-from-Home (WFH)

├── Is your status for AM or PM?
│
├── AM
│   └── What is your PM Status
│
└── PM
    └── What is your AM Status

└── Acknowledgement of Status
```

---

# Summary Flow

```
START
 │
 ├── Name
 │
 ├── Department
 │
 └── Status
      │
      ├── Present (NSC)
      ├── Present (IS)
      ├── Deployment
      ├── Other Deployment
      ├── MA
      ├── MC
      ├── HL
      ├── Annual Leave
      ├── Overseas Leave
      ├── OIL/PHIL
      ├── Birthday Off
      └── WFH
             │
             └── Display relevant conditional questions