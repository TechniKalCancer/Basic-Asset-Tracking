# Asset Tracking System

This is a K-12-focused asset tracking system built with Flask, SQLAlchemy, and JavaScript — think a lightweight, self-hosted alternative to IncidentIQ for a school's Chromebook fleet (and chargers, iPads, hotspots, etc). It checks devices in/out with a full history, keeps a student/staff directory, bulk-assigns a whole roster to their devices at once, prints Dymo labels (with an optional second charger label), sends overdue-return email reminders, supports physical inventory audits, and logs damage incidents per student.

## Prerequisites

- Docker
- Docker Compose

## Setup

1. **Clone the repository:**

    ```sh
    git clone https://github.com/yourusername/asset-tracking-system.git
    cd asset-tracking-system
    ```

2. **Create a `.env` file:**

    Copy `.env.example` to `.env` and fill in real values:

    ```sh
    cp .env.example .env
    ```

3. **Build and run the Docker containers:**

    ```sh
    docker compose up --build -d
    ```

    This starts two containers: `db` (Postgres, the real database) and `web` (the app).
    `web` waits for `db`'s healthcheck before starting.

4. **Access the application:**

    Open your web browser and go to `http://localhost:8081`.

## Database (Postgres)

Data lives in a real Postgres database, not a throwaway file — the `db` service in
`docker-compose.yml`, persisted in the named volume `basic-asset-tracking_db-data` so it
survives container rebuilds/restarts.

- **Credentials**: set `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` in `.env` before
  first startup — Postgres only applies them when initializing a brand-new database, so
  changing them later won't update one that already exists (you'd need to recreate the volume).
- **Connect an external tool** (pgAdmin, TablePlus, DBeaver, `psql`, etc.): port `5432` is
  published to the host, so connect to `localhost:5432` with the credentials from `.env`.
- **`psql` via Docker directly**, no external tool needed:
  ```sh
  docker compose exec db psql -U asset_tracker -d asset_tracker
  ```
- **Back up**: `docker compose exec db pg_dump -U asset_tracker asset_tracker > backup.sql`
- **Restore**: `docker compose exec -T db psql -U asset_tracker asset_tracker < backup.sql`
- Running `app.py` directly (outside Docker, e.g. for local dev in a venv) still defaults to a
  local SQLite file unless you set `DATABASE_URL` yourself — the Postgres wiring only kicks in
  through `docker-compose.yml`'s `web` service.

### Schema migrations (Flask-Migrate / Alembic)

Schema changes are managed by [Flask-Migrate](https://flask-migrate.readthedocs.io/) (built on
Alembic), tracked in the `migrations/` folder. There's a single **baseline migration**
(`migrations/versions/ff757c46630a_baseline_*.py`) that creates the entire schema as of this
README update — before that, changes were applied by hand via `ALTER TABLE`, which is why
history doesn't go back further than one migration.

**How it runs:** `entrypoint.sh` runs `flask db upgrade` once, before gunicorn starts (see the
`Dockerfile`/`entrypoint.sh`) — not per-worker, and not via `db.create_all()` anymore. This
means:
- A **brand-new deployment** (empty database) gets every table created by replaying the full
  migration history from scratch — no manual setup step needed.
- An **existing deployment** just applies whatever migrations it hasn't seen yet. Already
  up to date → it's a no-op and boots normally.

**Making a schema change going forward:**

1. Edit the model(s) in `app.py`.
2. Generate a migration script from the diff:
   ```sh
   docker compose exec web flask db migrate -m "short description of the change"
   ```
   (or run `flask db migrate` locally against a Postgres instance if not using Docker — Alembic
   needs a live DB connection to autogenerate the diff, unlike a plain model edit.)
3. **Read the generated file in `migrations/versions/`** before committing — autogenerate is
   usually right but doesn't always guess renames/complex changes correctly.
4. Commit the migration file alongside the model change. It applies automatically on the next
   deploy via `entrypoint.sh`'s `flask db upgrade` — no separate manual step, and no more
   copy-pasting `ALTER TABLE` statements into a deploy checklist.

**Always back up before a deploy that includes a schema change:**

```sh
docker compose exec db pg_dump -U asset_tracker asset_tracker > backup_$(date +%Y%m%d_%H%M%S).sql
```

**Local dev outside Docker** (`python app.py` directly, no `entrypoint.sh` involved): run
`FLASK_APP=app.py flask db upgrade` yourself once before starting the app, and again after
pulling any update that adds a new migration file.

## Project Structure

- `app.py`: The main Flask application file.
- `models.py`: Contains the SQLAlchemy models for the database.
- `static/`: Contains static files like CSS, JavaScript, and images.
- `templates/`: Contains HTML templates for rendering the web pages.
- `Dockerfile`: Docker configuration file for building the Docker image.
- `docker-compose.yml`: Docker Compose configuration file for setting up the services.
- `.env`: Environment variables file (not included in the repository).
- `.dockerignore`: Specifies which files and directories to ignore when building the Docker image.
- `.gitignore`: Specifies which files and directories to ignore in the Git repository.

## API Endpoints

- `GET /api/assets`: Retrieve all assets.
- `POST /api/assets/<asset_id>`: Check in or check out an asset.
- `GET /asset_history`: Retrieve the history of actions for a specific asset.

## Adding Devices (manual or CSV)

Asset tags aren't strictly required anymore — leave one blank and the system self-assigns a
random unique 6-digit tag:

- `/admin/registry/new` ("+ Add Device" on the registry page): add one device by hand. Leave
  Asset Tag blank to auto-generate one, or type your own (checked for uniqueness).
- CSV import (`/admin`): every column is now optional. A row with no `asset_tag`/`asset_id`
  falls back to using `serial_number` as the tag (unchanged prior behavior); a row with
  neither gets a self-assigned 6-digit tag. A CSV can even omit the `asset_tag` column
  entirely — every row just gets a generated tag (handy for a batch of chargers or other
  accessories you haven't physically labeled yet).

## People & Asset Assignment

- `/admin/people`: List/search people. Each person has a first name, last name, email, role (staff/student), optional department, optional site (school/building — e.g. "North Elementary"), and an optional **ID Number** (`external_id` — the district staff/student ID). Search matches name, email, site, or ID number. A `show` filter (`active` / `inactive` / `all`) controls whether graduated/withdrawn people appear (see Graduating Students below).
- `/admin/people/new`, `/admin/people/<id>/edit`: Manage people individually.
- `/admin/people/<id>/delete`: **Permanently** deletes a person — any assets they hold are unassigned first, and their `AssignmentHistory`/`Incident` rows are kept (with a name snapshot) but unlinked from the deleted record. Prefer graduating students instead of deleting them, since deleting removes them from every list with no undo. Reserve delete for genuine data-entry mistakes (e.g. a duplicate record).
- `/admin/assets/<asset_tag>/assign`: Assign or reassign an asset (must already exist in the registry) to a person. The "Assign to" field is a live search-as-you-type picker (backed by `/admin/people/search`, capped at 20 results) instead of a dropdown listing every person — it stays fast with thousands of people in the system, and only ever offers active (non-graduated) people. Results show the matching name, site, and email so you can tell duplicate names apart at a glance.
- `/admin/assets/<asset_tag>/unassign`: Clear an asset's assignment.
- The asset registry page (`/admin/registry`) shows each asset's current assignee and links to assign/reassign it.

### Bulk Import / Update People (`/admin/people/import`)

Upload a district roster CSV to create new people and **update existing ones in bulk** — the ID
Number is the key that makes this possible. Each row is matched to an existing person by
`external_id` first (falling back to `email` if no ID number is given or matched); a match updates
that person's fields, no match creates a new person. Existing people missing from the CSV are left
alone — unlike the asset registry import, this never wipes anything.

Required columns: `first_name`, `last_name`, `email`. Optional: `external_id` (also accepts
`staff_id`/`student_id`), `role` (staff/student), `department`, `site`, `grad_year` (also accepts
`graduation_year`). Re-running this each semester with a fresh SIS export is the intended workflow
for keeping site/department/grade assignments current without hand-editing hundreds of records.

### Graduating Students (`/admin/people/graduate`)

Removes a whole graduating class from the active roster in one action, instead of deleting
students one at a time. Pick a graduation year (set via the person form or bulk import); every
active student with that `grad_year` gets any checked-out devices automatically unassigned, then
is archived (`is_active = False`) — **not deleted**. Archived people:
- Disappear from the default `/admin/people` list and from the assign-device search, so a
  graduated student can't accidentally end up with a new device.
- Keep their full assignment history and incident/fee records intact, since those stay linked to
  the (now-archived) person record — useful for chasing down a damage fee after the fact.
- Can be viewed via `/admin/people?show=inactive` and undone anytime with the **Reactivate**
  button, e.g. if a student was graduated by mistake.

## Assignment History, Condition Notes & Status

- Every assign/unassign is logged to `AssignmentHistory` — who had an asset, from when to
  when, plus optional condition notes captured at checkout and at return. Reassigning to a
  new person automatically closes out the previous history row. View it on the assign page
  (`/admin/assets/<asset_tag>/assign`).
- Each asset has a `status`: `available`, `assigned`, `repair`, `lost`, or `retired`.
  Assigning/unassigning sets it automatically (`assigned` / `available`), or it can be set
  directly via `POST /admin/assets/<asset_tag>/status` for cases like marking something
  in for repair — independent of who (if anyone) it's assigned to.
- The registry page shows each asset's current status as a color-coded badge.
- Each asset also has a `device_type` (`chromebook`, `laptop`, `ipad`, `charger`, `hotspot`,
  `other`) — settable via an optional `device_type`/`type` column in the CSV import, filterable
  on the registry page. The fleet doesn't have to be all Chromebooks.

## Bulk Assign & Bulk Print (the "hand out the whole roster" workflow)

- `/admin/bulk_assign`: upload a CSV of `asset_tag,email,due_date` to assign a whole class or
  grade to their devices in one shot, instead of one-at-a-time. People must already exist (add
  them via `/admin/people` or before this step) — a typo'd email is reported as an error for
  that row rather than silently creating a bad record. Shows a per-row success/failure summary.
- `/admin/bulk_print`: checkbox-select devices (defaults to everything currently `Assigned`,
  i.e. what you just bulk-assigned) and print all their labels in one sitting — the SDK call
  loops over the selection with a short delay between each print job.
- Both the single-asset Print Label card and Bulk Print have an **"Also print charger
  label(s)"** toggle — prints a second label per device (same asset tag + name, marked
  "— CHARGER") for zip-tying to the charger, since chargers wander off independently of the
  device they belong to.

## Access Control & Kiosk Mode

The whole site requires admin login by default — including Home, Check In, Check Out, History,
and the API endpoints. The one exception is devices enrolled as a kiosk:

- `/admin/kiosk`: Lists enrolled kiosk devices. Log in and load this page **on the device you
  want to enroll** (e.g. a front-desk Chromebook), then use "Enroll This Device" — this sets a
  long-lived cookie on that browser only.
- An enrolled device can use `/`, `/checkin`, `/checkout`, and `/api/scan` without logging in.
  Everything else (People, registry, history, admin API, other kiosk devices) still requires
  admin login on that same device.
- Revoking a device (from the `/admin/kiosk` list, on **any** logged-in admin session — not
  just the kiosk itself) deletes its token, which locks it out immediately on its next request.
  Useful if a kiosk machine is lost or compromised.

## User Accounts & Permissions

Beyond the single shared `ADMIN_PASSWORD` (which always logs in as a full superuser — leave the
username field blank), you can create named accounts with limited access:

- `/admin/users` (superuser only): create/edit/delete named accounts. Each one has a username,
  password, and a checkbox per area — **People**, **Devices** (the asset registry/assign flow),
  **Loaners**, **Repairs** — plus an **Admin** checkbox for a second full superuser. Unchecking
  "Active" on an existing user disables their login without deleting the account.
- Log in with a username to use a named account instead of the shared password.
- Routes are gated server-side by area (e.g. `/admin/people*` requires People, `/admin/loaners*`
  requires Loaners), and the nav bar / dashboard quick-links hide whatever a user can't access —
  so a Loaners-only account never even sees a People link to click.
- A handful of sensitive, cross-cutting operations (CSV registry replace, bulk assign, kiosk
  device management, reminder settings) are superuser-only regardless of the People/Devices/
  Loaners checkboxes, to keep the permission model simple.

## Global Search

Every admin page has a search box in the nav bar (`/admin/search`). It checks People and Assets
at once — tag, serial, description, name, email, site, ID number. A single unambiguous match
jumps straight to that record (a person's device list, or an asset's detail page); multiple
matches show a combined results page for both.

## Loaner Devices

A separate short-term checkout system for a pool of spare devices, distinct from the main
per-student assignment workflow:

- From `/admin/registry` or `/admin/loaners`, mark any device as a loaner ("Mark Loaner"). It
  stays in the regular registry too — being a loaner doesn't remove it from anything else.
- `/admin/loaners`: shows the whole loaner pool with current status, and lets staff check a
  loaner out to (or back in from) any person directly.
- `/loaner_checkout` and `/loaner_checkin`: public self-service pages (same kiosk-or-login access
  as Check In/Check Out) — a student searches for their own name, then scans/types the loaner's
  asset tag or serial. Checkin just needs the asset tag/serial; the system already knows who has
  it. Leaving the due date blank defaults to a 7-day loan.
- Overdue loaners get an email automatically once an hour (if `SMTP_USERNAME`/`SMTP_PASSWORD` are
  set) — no one has to remember to check. There's also a "Send Reminders Now" button on
  `/admin/loaners` for an immediate resend without waiting for the next hourly pass.

## Filters, Export & Dashboard

- `/admin/registry` has a status filter dropdown alongside the existing tag/serial/description
  search, so you can view e.g. just assets currently `In Repair`.
- `/admin/registry/export` (also linked as "Export CSV") downloads the **full** asset list —
  not just the current page/filter — as a CSV with asset tag, serial, description, status, and
  current assignee. Useful as a point-in-time backup or for reporting outside the app.
- The admin panel's "Assets by Status" card shows a live count per status; clicking a count
  jumps straight to that filtered registry view.

## Dymo Label Printing

The assign page (`/admin/assets/<asset_tag>/assign`) has a "Print Label" card that prints the
asset tag and assigned person's name — plus a Code 128 barcode of the asset tag on the right
half of the label, so it can be scanned back in by Check In/Check Out, the registry's scan
lookup, or an audit — to a Dymo LabelWriter (built/tested against a 550 Turbo) using the
official [DYMO Connect Framework](https://github.com/dymosoftware/dymo-connect-framework)
JavaScript SDK, vendored at `static/js/dymo.connect.framework.js`.

**On each machine you want to print from, one-time setup:**

1. Install [DYMO Connect](https://www.dymo.com) (the desktop app) — it runs a local web
   service on `127.0.0.1:41951` (or up to `41960` if that port's busy) that the browser talks to.
2. With DYMO Connect running, visit `https://127.0.0.1:41951` in the same browser once and
   accept the self-signed certificate warning. Skipping this is the most common reason printing
   silently fails — the SDK calls just hang until the timeout.
3. Load `/admin/assets/<asset_tag>/assign` — the Print Label card should detect DYMO Connect
   within a few seconds, list your printer(s), and show a live preview.

**Label stock**: built for **30252 Address** (1-1/8" × 3-1/2"), text on the left half and the
barcode on the right half. To use a different label size, edit `DYMO_LABEL_XML` in
`static/js/dymo_label_core.js` (shared by both single and bulk printing) — swap `PaperName` and
the `DrawCommands` geometry for your stock (DYMO Connect's own label designer can export the XML
for any label you create there), and keep the `AssetTag`/`PersonName`/`Barcode` object names
as-is since `buildAssetLabel()` targets them by name via `label.setObjectText()`. The barcode
always encodes the plain asset tag (no "— CHARGER" suffix), so scanning either the device's or
the charger's printed label resolves to the same asset.

I haven't been able to test actual printing end-to-end since I don't have Dymo hardware in
this environment — the SDK calls follow DYMO's own official sample code exactly, but printing
itself needs to be verified on your machine with the printer attached.

## Barcode Scan Lookup

`/admin/registry` has a "Scan or type an asset tag/serial" field at the top. USB barcode
scanners work here (and on Check In/Check Out) with no special integration — they just act as
a keyboard, typing the scanned code into whichever field is focused and sending Enter. Scanning
here resolves the code (via the same lookup used by Check In/Check Out) and jumps straight to
that asset's assign page, for fast physical intake/audit workflows.

## Check-In/Out Log Tied to the Student

Check In/Check Out (`/checkin`, `/checkout`, `/api/scan`) now snapshots whoever the asset was
assigned to at the moment of the scan onto the `Event` row. The assign page shows a "Check-In/Out
Log" card and `/asset_history` shows a Person column, so day-to-day scan activity reads as "who
had it," not just an anonymous asset-tag log.

## Asset Audit (physical inventory verification)

A real, commonly-cited problem with school device fleets is that a meaningful chunk of assets
end up not properly tracked or located — one state audit found roughly a fifth of IT assets
improperly accounted for. `/admin/audit` is a lightweight fix for that:

- Set an audit start date (defaults to today) and optionally filter by status/device type.
- Scan every device you physically find — each scan logs an `AuditScan` row.
- Anything in scope that hasn't been scanned since the start date shows up as **missing** — the
  actionable "go find these" list, instead of only discovering losses at year-end collection.

## Incident / Damage Reports

`/admin/assets/<asset_tag>/incidents` logs a damage/loss report against an asset, snapshotting
whoever it's currently assigned to. The assign page shows the full incident history for that
asset and — matching the progressive accountability policies many districts already use (e.g.
1st incident free, 2nd billed, 3rd billed plus discipline) — flags what number incident this
would be for the currently assigned student, counted across all their devices, all time.

## Overdue Reminders (Google SMTP)

Assigning an asset now has an optional **Due Date**. `/admin/reminders` lists every open
assignment whose due date has passed and lets you email each assigned person a reminder.

- Configure `SMTP_USERNAME` / `SMTP_PASSWORD` in `.env` — see the comments in `.env.example`
  for the Gmail App Password setup steps (2-Step Verification must be on first). Leaving them
  blank is fine; the reminders page still shows what's overdue, the send button just stays
  disabled.
- `POST /admin/reminders/send` emails everyone currently overdue in one batch, skipping
  gracefully (with a summary count) if a person was deleted or a send fails, and records
  `reminder_sent_at` per assignment so you can see who's already been nudged.
- Sending is always a manual, explicit click on that page — nothing emails anyone automatically
  in the background.
- I couldn't test an actual send in this environment (no real Gmail credentials configured
  here) — the SMTP logic is standard `smtplib` STARTTLS + login, but do a real test send once
  you've set your credentials.

## Google Workspace Sync (Stage 2 — framework only, not yet implemented)

The scaffolding for syncing Chromebook info (model, org unit, recent user) from Google
Workspace by serial number is in place, but the actual Admin SDK call is not implemented yet.

- `GOOGLE_SERVICE_ACCOUNT_FILE` / `GOOGLE_ADMIN_IMPERSONATE_EMAIL` (see `.env.example`) control
  whether sync shows as "Configured" in the admin panel. Leaving them blank is fine — the rest
  of the app works normally, the sync button just stays disabled.
- `Asset.google_model`, `google_org_unit`, `google_recent_user`, `google_last_sync_at` columns
  already exist to hold synced data once it's wired up.
- `sync_chromeos_device_from_google(serial_number)` in `app.py` is the function to fill in —
  it currently raises `NotImplementedError`. It needs a Google Cloud service account with
  domain-wide delegation authorized (in the Workspace Admin console) for the
  `admin.directory.device.chromeos.readonly` scope, calling the `chromeosdevices` resource
  of the Admin SDK Directory API while impersonating a super admin.
- `/admin/assets/<asset_tag>/google_sync` (POST) is already wired to call it and store the
  result — no route/UI changes should be needed to activate Stage 2, just that one function.

## Purchase & Warranty Tracking

Each registry entry can optionally hold a **purchase date**, **purchase cost**, and
**warranty expiration** — set them when adding/editing a device (`/admin/registry/new`,
`/admin/registry/<tag>/edit`) or via CSV import (optional `purchase_date`, `purchase_cost`/`cost`,
`warranty_expiration`/`warranty` columns). The registry page shows a **Warranty Expiring**/
**Warranty Expired** badge on affected devices and a `?warranty=expiring`/`?warranty=expired`
filter; the admin dashboard shows a **Warranty Expiring Soon** stat card (devices whose warranty
runs out within 60 days) linking straight into that filter. CSV export includes all three columns.

## Repairs (RMA Tracking)

A real tracking record for devices sent out for repair, separate from just labeling a device
`repair` in its status:

- From a device's assign page, **Send to Repair** (requires the **Repairs** permission) records
  vendor, ticket number, issue description, and expected return date, and sets the device's
  status to `repair` in the same step. A device's status can no longer be set to `repair`
  directly through the plain status dropdown — that now redirects you to Send to Repair instead,
  so a repair is never entered without a tracking record. If a device's status is changed away
  from `repair` some other way while a repair record is still open, that record auto-closes
  itself (matching how an open loaner checkout auto-closes on device delete).
- **Mark Returned** requires an outcome — **Fixed** (device goes back into service, `assigned`
  if it still has an owner or `available` otherwise), **Could Not Repair**, or **Replaced**
  (both retire the physical unit; adding its replacement is an ordinary separate "Add Device").
- `/admin/repairs`: fleet-wide list of open and recently-closed repairs, gated by a dedicated
  **Repairs** permission (`can_repairs` on a `User`) independent of Devices access — useful for
  a repair-desk-only account that shouldn't otherwise touch the registry.

## Fee Tracker

Incidents can now carry a dollar amount, not just a "fee charged" checkbox:

- Logging an incident (`/admin/assets/<asset_tag>/incidents`) accepts an optional **Fee Amount**
  — entering one automatically marks the incident as charged, regardless of the checkbox.
- `/admin/fees`: "who owes money" — every unpaid, charged incident grouped by person with a
  per-person subtotal and a grand total, each with a one-click **Mark Paid**.
- Deleting a person or graduating a class with unpaid charged incidents **warns but doesn't
  block** — the flash message notes the outstanding balance so it doesn't silently disappear,
  but the delete/graduate still goes through (matches how the rest of the app already handles
  similar situations rather than adding a new hard-stop).

## Self-Service: Report a Problem

`/report_problem` lets a student or staff member report a device issue themselves, without
needing a staff member to type it in — same kiosk-or-login access as Check In/Check Out/Loaner
Checkout. They search for their own name, scan or type the asset tag/serial, and describe the
problem; it's logged as an ordinary incident (no fee fields exposed — assessing a fee stays an
office decision made later from the device's assign page) snapshotting the *reporter's own*
identity, the same self-service pattern the Loaner Checkout page already uses.

## Activity Log

An accountability trail of who changed what: `/admin/activity` lists admin-side mutations
across People, Devices, Loaners, Users, Sites, Kiosk, Incidents/Fees, and Repairs, each row
showing when it happened, who did it (a named user, the shared admin login, an enrolled kiosk,
or "System" for the automated overdue-reminder background job), the action type, and a
human-readable summary. Filterable by actor, action, and date range, paginated 50/page. A
site-scoped admin only sees rows tied to one of their own sites; district-wide actions (Users,
Sites) are super-admin-only. This deliberately does **not** log every `/api/scan` check-in/out —
that volume is already fully captured by the existing `Event` table; this log exists for the
things nothing else records an actor for.

## Google Sync: Loaner Auto-Disable / Auto-Enable

Building on the Google Workspace Sync scaffolding above, a loaner Chromebook can be
automatically **disabled in Google the moment it's checked in**, and **re-enabled the moment
it's checked out** — so a student can't keep using a loaner after handing it back in.

- This is a separate, bigger-blast-radius opt-in on top of the read-only sync: it needs its own
  **write-scope** Google service account (`admin.directory.device.chromeos`, not `.readonly`)
  with domain-wide delegation authorized, and the `GOOGLE_LOANER_AUTO_DISABLE_ENABLED=true` env
  var set (see `.env.example`). Neither exists yet in this deployment — `set_chromeos_device_enabled()`
  in `app.py` currently raises `NotImplementedError`, same pattern as the read-only sync stub.
- Even with the env var and service account in place, it only runs for sites that have opted in
  via the **"Auto-disable loaner Chromebooks..."** checkbox on that site's Add/Edit form
  (Admin ▾ → Sites) — lets you pilot at one school before it's live everywhere.
- The check/act logic (`_sync_loaner_google_state()`) is wrapped so a Google-side failure only
  logs an error — it never blocks or rolls back the actual local checkout/checkin, and never
  raises back up to the person waiting on it.

## Navigation

The top nav is grouped into click-toggle dropdowns — **Devices ▾**, **People ▾**, **Loaners ▾**,
a standalone **Repairs** link, and **Admin ▾** (Dashboard, Kiosk Devices, Reminders, Activity
Log, plus Users/Sites for accounts with those permissions) — replacing the old flat link row and
the Admin Panel's button wall, both of which had grown past what a single row could hold. The
Reminders and Sites entries carry a small red badge with the current overdue-assignment/orphan
count. The Admin Panel dashboard itself keeps its data-driven content (stat tiles, per-site
breakdown, CSV upload) and promotes the overdue/orphan counts to alert banners at the top of the
page, so that urgency signal isn't lost now that the button wall is gone.

## Production Hardening

- **CSRF protection** (Flask-WTF `CSRFProtect`) on every state-changing request — every form
  carries a hidden `csrf_token`, and the one fetch-based endpoint (`/api/scan`) sends it via an
  `X-CSRFToken` header instead.
- **Debug mode is off unless explicitly enabled.** `FLASK_DEBUG=true` turns on Werkzeug's
  interactive debugger/reloader for local dev — never set this in production, it allows
  arbitrary code execution from a browser. `FLASK_ENV` controls a startup warning (logged, not
  blocking) if `SECRET_KEY`/`ADMIN_PASSWORD` are left at their dev fallback values.
- **Security headers** (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`) on every
  response. `SESSION_COOKIE_SECURE` is settable via `FORCE_HTTPS=true` once you're actually
  behind TLS.
- **Upload limits**: `MAX_CONTENT_LENGTH` caps request bodies at 16 MB, with a proper 413 error
  page instead of a bare connection reset.
- **Logging**: real `logging` (not `print()`) for errors, writing to stdout so it's captured by
  Docker/gunicorn/systemd logs.
- **Custom error pages** for 404/413/500 — no stack traces leak to the browser.
- **Docker**: gunicorn runs as a non-root user with 3 workers, a 60s timeout, and access logs to
  stdout; `docker-compose.yml` persists the SQLite file in a named volume.
- SQLite is fine at typical school scale; `DATABASE_URL` already supports Postgres
  (`psycopg2-binary` is in `requirements.txt`) if you outgrow it or run many gunicorn workers
  under heavy concurrent write load.

Sources for the K-12-specific features above (audit mode, incident tracking, accessory/charger
tracking): [IncidentIQ's Chromebook management overview](https://www.incidentiq.com/school-asset-management-software/chromebook-management-software),
[an NY State Comptroller audit finding ~22% of district IT assets improperly tracked/located](https://www.osc.ny.gov/local-government/audits/statewide-audit/2023/03/16/it-asset-management-2022-ms-2),
and [IncidentIQ's own writeup of common Chromebook damage-fee policies](https://www.incidentiq.com/blog/school-chromebook-policy-tips-fees-for-damaged-devices-and-more).
