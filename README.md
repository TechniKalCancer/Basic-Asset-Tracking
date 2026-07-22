# Asset Tracking System

This is a simple asset tracking system built with Flask, SQLAlchemy, and JavaScript. It allows you to check in and check out assets, view the history of asset actions, keep a directory of people, and assign assets to them.

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
    docker-compose up --build
    ```

4. **Access the application:**

    Open your web browser and go to `http://localhost:8081`.

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

## People & Asset Assignment

- `/admin/people`: List/search people. Each person has a first name, last name, email, role (staff/student), and optional department.
- `/admin/people/new`, `/admin/people/<id>/edit`, `/admin/people/<id>/delete`: Manage people. Deleting a person unassigns (rather than blocks on) any assets they held.
- `/admin/assets/<asset_tag>/assign`: Assign or reassign an asset (must already exist in the registry) to a person.
- `/admin/assets/<asset_tag>/unassign`: Clear an asset's assignment.
- The asset registry page (`/admin/registry`) shows each asset's current assignee and links to assign/reassign it.

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
