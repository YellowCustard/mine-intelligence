# Questions for Derek — RAN Mines (Randmine)

**Purpose:** the information and access Mine Monitor needs from the mine owner to price and
build Phases 1–4 honestly. Ordered so the **blockers come first**. Post to the WhatsApp
general group; Shayne to call Derek. Prepared following the requirements meeting.

> Nothing below can be firmly priced or promised until the **NDA is signed** and **Alhua API
> access** is granted — those two unblock almost everything else.

## A. Access & legal (blockers — do these first)
1. **NDA** — can we get the NDA signed with you (and with Heath) so we can be granted system
   access? Nothing deeper starts without it.
2. **Alhua camera API** — please authorise Heath to grant the **private/"backdoor" API** for
   the 118 cameras, plus **NVR admin login** and access to the NVR's existing **AI reports /
   vehicle-tracking** features (currently unused). API docs / SDK if any.
3. Who is our **on-site point of contact** for the server room, network and cameras day to day?

## B. Cameras & coverage
4. Is there a **site/camera layout map** showing where the 118 cameras are and what each sees?
5. Which cameras cover the **high-value / high-risk areas** — gold room, elution/CIL circuit,
   crusher discharge, weighbridge, gates, muster points? Any known **blind spots**?
6. For the personnel-tracking phase we need cameras with **overlapping coverage** on the paths
   people take between zones — where is coverage continuous vs patchy?
7. Are cameras on a network we can reach from an on-site server, or isolated to the NVR only?

## C. People, shifts & tags (Phase 1)
8. What **rostering / timesheet system** do you use, and can we read the schedule (so entry is
   allowed only for people who are actually on shift, and early/late arrivals are flagged)?
9. The **facial recognition at the gate** — which system/vendor, and can it emit an **event**
   (person, gate, time, granted/denied) we can consume? (We only need the event, never the
   face image or template.)
10. The **iHUA visitor tag** — what is it (BLE/RFID?), and can we read its per-area presence?
11. For each restricted **sector**, what is the **expected headcount / capacity** (so "6 people
    in a 5-person zone" can raise a breach)?
12. Are you open to a **physical tag** (RFID wristband or tear-off strap) alongside cameras?
    Vision alone will not meet a 99.9% bar; vision **+ tag** is how we get there reliably.

## D. Laboratory (Phase 4, but data helps reconciliation now)
13. The **Agilent 2000-series spectrometer** — can its software (SpectrAA) **export results to a
    file/CSV** automatically, and what PC/OS drives it? Is that PC networked?
14. Who is allowed to **edit a lab result** today, and where does it go after the instrument?
15. Can we have **3–6 months of the existing reconciliation spreadsheets** (head grade, tails,
    carbon, pour) to baseline what "normal" variance looks like?

## E. Generators & process (Phases 3–4)
16. The **5 DSE (Deep Sea Electronics) generators** — which controller model, and are they on a
    network we can read (Modbus / DSE Gateway) for fuel and run-hours?
17. The **weightometers** on the crusher — can we read their output directly (protocol/format)?
18. Conveyor **throughput / tank-level** data sources? (Note: we can *monitor and alert* on
    these; automated belt-speed control is a different, safety-certified product — see §G.)
19. The **pressurised gold-concentrate boxes** — where are the likely **leak points**, and do
    the boxes/liquid run at a temperature different from the surrounding equipment? (Decides
    whether infrared leak-detection is viable vs a camera-labelling approach.)

## F. Infrastructure
20. **Server room relocation** — the current container is next to a kitchen; where can a proper,
    cooled, powered server/edge room go? This gates the AI compute.
21. Confirm there is **one Starlink** link and that **all processing must stay on-site** (this is
    already how we design — just confirming there is no other backhaul).
22. Site **power** — mains reliability, generator backup, UPS in the server room?

## G. Commercial / acceptance
23. The **99.9% accuracy** requirement — can we agree this is measured **per capability** against
    a **one-month learning/validation period on your data**, not a single day-one number? Some
    things (count reconciliation) can be made very reliable; single-camera identity through
    masks and dust cannot, which is why we pair vision with a tag.
24. Confirmed understanding: **every alert warns a person; nothing here stops a machine** (no
    automated conveyor/vehicle control) — is that acceptable for Phase 3?
25. **Perimeter map** (for the Phase 2 drone/perimeter work) — when can we have it?

---
*Prepared for RAN Mines (Randmine), Bindura. Internal — send via the WhatsApp general group;
Shayne to call Derek.*
