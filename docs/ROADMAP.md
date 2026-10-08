# FoxDesk roadmap — self-hosted product for other districts

Decided 2026-10-08. Self-hosted only for now (each district runs its own copy).

**One person = one profile.** Google, Active Directory, Entra and PowerSchool
accounts attach to an existing person as extra identities (emails, usernames,
IDs) — they never create a second profile. Devices work the same way: one
device record, with its Google / AD / Entra / Intune / Jamf / KACE records
attached. **Every integration and most features can be switched off**, so a
district only sees what it uses.

## Phase 1 — Foundation
- [x] Test suite (pytest, real migrations, fresh DB per test) + GitHub Actions
- [x] Split `app.py` into the `foxdesk/` package (behavior and URLs unchanged)
- [ ] Module on/off switches (Settings → Features)
- [ ] Identity model: person identities + device directory records; migrate existing data

## Phase 2 — Directories
- [ ] Active Directory (LDAPS): users → identities on existing people; computers → device records
- [ ] Entra ID (Microsoft Graph): users + devices
- [ ] Intune (Graph managed devices) and Jamf Pro (API)
- [ ] Join-status view: AD-joined / Entra-joined / hybrid / Workgroup (joined to nothing)

## Phase 3 — PowerSchool roster sync
- [ ] OneRoster 1.1 CSV + API from PowerSchool; match students by student ID, never by name

## Phase 4 — Sign-in
- [ ] Single sign-on with Google and Microsoft (OIDC)
- [ ] Two-factor (TOTP) for local accounts; retire the shared admin password

## Phase 5 — Features
- [ ] Assign-from-Google-sign-in review page (propose holder per unassigned device, approve in batches)
- [ ] Parts inventory + Dell/Lenovo warranty lookup and claims
- [ ] Scheduled report emails (e.g. weekly summary per principal)

## Phase 6 — Ship-ready
- [ ] Privacy: policy page, data retention/purge schedule, SDPC NDPA template
- [ ] Installer: compose with HTTPS, first-run setup wizard, backup/restore in Settings
- [ ] Pinned dependencies, Python 3.12 image, versioned releases + changelog
- [ ] Accessibility audit (WCAG 2.1 AA) + VPAT
