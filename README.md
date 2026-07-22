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

- `/admin/people`: List/search people. Each person has a first name, last name, email, role (staff/student), and optional department.
- `/admin/people/new`, `/admin/people/<id>/edit`, `/admin/people/<id>/delete`: Manage people. Deleting a person unassigns (rather than blocks on) any assets they held.
- `/admin/assets/<asset_tag>/assign`: Assign or reassign an asset (must already exist in the registry) to a person.
- `/admin/assets/<asset_tag>/unassign`: Clear an asset's assignment.
- The asset registry page (`/admin/registry`) shows each asset's current assignee and links to assign/reassign it.

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
asset tag and assigned person's name to a Dymo LabelWriter (built/tested against a 550 Turbo)
using the official [DYMO Connect Framework](https://github.com/dymosoftware/dymo-connect-framework)
JavaScript SDK, vendored at `static/js/dymo.connect.framework.js`.

**On each machine you want to print from, one-time setup:**

1. Install [DYMO Connect](https://www.dymo.com) (the desktop app) — it runs a local web
   service on `127.0.0.1:41951` (or up to `41960` if that port's busy) that the browser talks to.
2. With DYMO Connect running, visit `https://127.0.0.1:41951` in the same browser once and
   accept the self-signed certificate warning. Skipping this is the most common reason printing
   silently fails — the SDK calls just hang until the timeout.
3. Load `/admin/assets/<asset_tag>/assign` — the Print Label card should detect DYMO Connect
   within a few seconds, list your printer(s), and show a live preview.

**Label stock**: built for **30252 Address** (1-1/8" × 3-1/2"). To use a different label size,
edit `DYMO_LABEL_XML` in `static/js/dymo_label_core.js` (shared by both single and bulk
printing) — swap `PaperName` and the `DrawCommands` geometry for your stock (DYMO Connect's own
label designer can export the XML for any label you create there), and keep the two
`TextObject` names (`AssetTag`, `PersonName`) as-is since `buildAssetLabel()` targets them by
name via `label.setObjectText()`.

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
