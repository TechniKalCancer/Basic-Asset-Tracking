# Icons

Drop finished icons into this folder as `<name>.svg` (exact names below) and
redeploy/restart — every page picks them up automatically. Until a file exists,
that spot just shows its text label, so icons can be added a few at a time.

## Spec

- **SVG**, `viewBox="0 0 24 24"`, artwork kept inside a 20×20 live area (2px padding).
- **One color, transparent background.** The app draws each icon as a mask and
  fills it with the surrounding text color, so it automatically matches the theme
  and branding colors — any color in the file is ignored, and multi-color detail is lost.
- **Consistent style across the set** — e.g. 2px rounded strokes (outline style),
  or all solid glyphs. Must read clearly at 16px.
- No text inside icons, no embedded images, no scripts. Run through SVGO if possible.
- File name = the name in the tables below, lowercase with hyphens.

## Priority 1 — navigation and everyday controls (16)

| File | Meaning | Where it shows | Suggested metaphor |
|---|---|---|---|
| `dashboard.svg` | Dashboard | Top nav tab, Dashboard heading | gauge / grid of tiles |
| `devices.svg` | Devices / registry | Top nav tab, Registry, Asset History | laptop / Chromebook |
| `people.svg` | People | Top nav tab, Bulk Assign | two people |
| `loaner.svg` | Loaner device | Loaners tab, loaner badges everywhere | backpack, or laptop with a return arrow |
| `repair.svg` | Repairs | Repairs tab, repair cards | wrench + screwdriver |
| `ticket.svg` | Help-desk tickets | Tickets tab, ticket cards, kiosk tile | ticket stub |
| `settings.svg` | Admin / settings | Admin tab, Settings page | gear |
| `search.svg` | Search | Nav search button, Search Results, 404 page | magnifying glass |
| `help.svg` | Help | Help link, Help page, Help Content | question mark in a circle |
| `menu.svg` | Open menu | ☰ button on phones/tablets | three horizontal lines |
| `scan.svg` | Scan a barcode | Scan/lookup fields, Check In/Out tab | barcode with scan line |
| `camera.svg` | Scan with camera / add photo | Camera button on scan fields, photo uploads | camera |
| `check-in.svg` | Check In | Kiosk home tile | arrow into a box/tray |
| `check-out.svg` | Check Out | Kiosk home tile | arrow out of a box/tray |
| `flag.svg` | Report a problem / possible violation | Kiosk tile, Report a Problem, Possible Violators, Sign-in Mismatches | flag |
| `warning.svg` | Warning | Security banner, orphans, error page | triangle with ! |

## Priority 2 — actions (9)

| File | Meaning | Where it shows | Suggested metaphor |
|---|---|---|---|
| `add.svg` | Add / new | Add Device, New Ticket, new mappings | plus |
| `edit.svg` | Edit | Edit buttons on devices, tickets, people | pencil |
| `delete.svg` | Delete | Delete actions | trash can |
| `print.svg` | Print | Label printing, invoices | printer |
| `download.svg` | Export / download CSV | Registry export, Data Quality CSV | arrow down onto a line |
| `import.svg` | Import / upload CSV | People import, registry CSV upload | arrow up from a tray |
| `email.svg` | Email / notify | Email settings, reminders, guardian notices, "emailed" badges | envelope |
| `sync.svg` | Sync | Google/KACE sync buttons, Scheduled Syncs | two circular arrows |
| `success.svg` | Done / confirm | Mark Returned, Enable in Google, "nothing to review" | check mark in a circle |

## Priority 3 — feature areas (24)

| File | Meaning | Where it shows | Suggested metaphor |
|---|---|---|---|
| `audit.svg` | Asset Audit (physical inventory) | Asset Audit page | clipboard with check marks |
| `data-quality.svg` | Data Quality checks | Data Quality page | shield or magnifier with a check |
| `history.svg` | History / activity log | Activity Log, assignment history, recent activity | clock with counter-clockwise arrow |
| `billing.svg` | Fees / billing | Billing page, ticket and repair charges | dollar bill / receipt |
| `site.svg` | School / site | Sites, site breakdowns, site picker | school building |
| `computer.svg` | Computer / kiosk / KACE | Kiosk Devices, Device Models, KACE pages | desktop monitor |
| `graduate.svg` | Graduate students | Graduate Students page | graduation cap |
| `automation.svg` | Ticket automations | Automations, pending automated actions | lightning bolt or gear-with-spark |
| `custom-fields.svg` | Custom fields / field mapping | Custom Fields, Google/KACE field mapping | form with list rows |
| `integration.svg` | Google / external connection | Google Workspace info and setup, KACE | two linked chain links / plug |
| `org-unit.svg` | Google org units | Org Units, Push to Org Units | folder tree |
| `schedule.svg` | Scheduled / reminders | Scheduled Syncs, Overdue Reminders | alarm clock |
| `branding.svg` | Branding | Branding page | paint palette |
| `users.svg` | Staff user accounts | Users & Permissions | key or person with badge |
| `person.svg` | A single person | Person rows, search results | single person |
| `protection-plan.svg` | Device protection plan / insurance | Badges on people with the plan | shield |
| `collection.svg` | End-of-year collection | Collection (Who Has What) | box / package |
| `category.svg` | Categories | Ticket Categories | price tag |
| `asset-number.svg` | Asset tag number ranges | Asset Number Ranges | hash / number tag |
| `lock.svg` | Login / locked | Login page, Loaner Auto-Disable badge | padlock |
| `disable.svg` | Disable device | Disable in Google button | circle with slash |
| `profile-clear.svg` | Clear user profiles from device | Profile Clear button | broom or user with an x |
| `document.svg` | File / PDF attachment | Non-image attachments | page with folded corner |
| `logo.svg` | Fallback app mark | Nav brand, only when no logo is uploaded under Branding | your app mark (optional) |
