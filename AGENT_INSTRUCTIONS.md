# Build a simple personal timesheet web app

You are the implementation agent. Build and verify the working application described below in this repository. Do not merely return a plan or sample snippets.

## Goal and scope

Build a mobile-friendly, single-user application for recording arrival and departure at work, correcting forgotten entries, and viewing monthly hours. The user will deploy it with Docker Compose behind their own NGINX reverse proxy and handle HTTPS themselves.

Keep it small: one application container, SQLite, and no separate database, SPA build pipeline, cloud service, or external API. Do not add payroll, employee management, GPS, notifications, or automatic break deductions.

Default implementation: Python with Flask, server-rendered HTML/Jinja, lightweight CSS, a small amount of JavaScript only where useful, Python's SQLite library, Gunicorn, and pytest. If repository conventions already dictate another simple stack, follow them and explain the deviation. Use supported dependency versions and record reproducible dependencies; do not invent versions.

## 1. Home screen

- Make the main screen usable on a phone, with two large, clearly labeled `IN` and `OUT` buttons, readable text, and comfortably sized touch targets. Do not communicate status through color alone.
- Show the current status: `Not at work` or `At work since …`, the configured timezone, today's recorded hours, and recent entries.
- `IN` records the server's current timestamp and creates an open work interval.
- `OUT` closes the open interval using the server's current timestamp.
- Disable `IN` while an interval is open and disable `OUT` otherwise. Enforce these rules on the server too.
- Permit multiple intervals in one day, such as leaving for lunch and returning.
- Show an explicit success message after a save and a helpful error on failure. Never display success before the server confirms persistence.
- Prevent duplicate records from double taps, retries, or two browser tabs. Apply atomic database transactions and database constraints, not just disabled UI controls. Make replayed form submissions idempotent using an operation token or equivalent mechanism.
- Refreshing the page must not repeat an action; use POST/redirect/GET.

## 2. Manual entry and corrections

Provide a clearly visible `Add / correct times` section.

- Allow adding a completed interval with local start date/time and local end date/time.
- Allow adding a missing IN as an open interval, and closing an existing open interval with a manually selected OUT time.
- Allow editing either endpoint of an existing interval and deleting an accidental interval after confirmation.
- Prefill dates sensibly but allow dates in the past. Label fields explicitly, including the end date, so overnight work is not ambiguous.
- Validate on the server: required fields, valid dates, end strictly after start, no future endpoints, no overlapping intervals, and at most one open interval. Adjacent intervals are allowed.
- For overlap checks, treat an open interval as continuing indefinitely until it is closed. Reject edits/additions that would conflict with it; explain that the user must close or correct it first.
- Preserve entered values after validation errors and show errors next to the relevant fields.
- Never silently overwrite, truncate, or repair existing records.

## 3. Monthly report

Provide a month/year selector, defaulting to the current month in the configured timezone.

- Display every calendar day of the selected month, including days without work.
- Columns: date, weekday, time IN, time OUT, and total hours for that day.
- Show each interval's endpoints on days with multiple intervals; a grouped layout or expandable interval rows is fine. Do not imply lunch breaks were paid by treating the earliest IN and latest OUT as one continuous interval.
- Clearly identify incomplete/open intervals. Do not count their unfinished duration in finalized daily or monthly totals. The home screen may show a separately labeled live elapsed duration.
- Sum completed intervals and show a prominent monthly total.
- Display durations as `HH:MM` with hours allowed to exceed 24. Optionally show decimal hours as a secondary value with a clear label. Sum exact durations first, then format totals; do not round each interval before adding.
- Split overnight intervals across local calendar days for daily totals. Split intervals crossing month boundaries so each month's total contains only time within that month. Mark continued intervals so clipped midnight boundaries are not mistaken for actual punches.
- Add a clean printable report using print CSS. A CSV download is a useful small addition: include daily totals, the monthly total, and interval detail sufficient to explain the totals. CSV and print must use the same calculations as the screen.
- An empty month must show zero totals without errors.

## 4. Time handling

- Make the application's IANA timezone configurable with `APP_TIMEZONE`. Use `Europe/Rome` as an editable example in `.env.example`, not as an inferred user location. Require/document the timezone choice before deployment; validate it at startup.
- Store timestamps consistently as UTC instants, preferably integer epoch seconds. Convert only at input/display and local-day reporting boundaries.
- Use the server clock for button punches, not client-submitted timestamps.
- Calculate elapsed time using UTC instants. Determine calendar-day and month boundaries in the configured timezone. Do not assume all local days are 24 hours.
- Reject nonexistent manual local times during daylight-saving transitions. For ambiguous local times, ask the user to choose the applicable UTC offset rather than guessing.
- Document that changing the reporting timezone changes date grouping, not the stored instants. Handle an interval left open overnight visibly; never auto-close it.
- Isolate the clock and time calculations so tests can use fixed timestamps without sleeping.

## 5. Storage and integrity

- Use a SQLite file at `DATABASE_PATH`, default `/data/timesheet.sqlite3` in the container.
- A minimal interval record needs an ID, UTC start, nullable UTC end, and creation/update timestamps. Add database constraints for valid endpoints and a partial unique index enforcing at most one open interval.
- Initialize a fresh database automatically and idempotently. Provide a small schema-version/migration mechanism if needed; never recreate a populated database at startup.
- Use parameterized SQL, appropriate transaction boundaries, a busy timeout, and SQLite settings suitable for the chosen single-container deployment. Check overlapping intervals inside the same serialized write transaction to avoid concurrent-write races.
- Persist all database-related files in the mounted data directory. Ensure the runtime user can write there.
- Give useful errors for invalid input, conflicting actions, and unavailable storage. Log diagnostic information without exposing it in the UI.

## 6. Deployment and security

Deliver `Dockerfile`, `compose.yaml`, `.dockerignore`, and `.env.example`.

- `docker compose up --build -d` must start a production application server, not Flask's development server.
- Listen on `0.0.0.0:8000` inside the container. Publish an easily configurable host port, default `8080`.
- Default the host binding to `127.0.0.1` for NGINX running on the host; document how to change it for a different proxy topology. NGINX in another container should reach the app over an appropriate Docker network, not through its own loopback address.
- Mount a named Docker volume at `/data`; data must survive container recreation. Set a restart policy and a health check with a lightweight `/health` endpoint. Health responses must not expose records or configuration secrets.
- Run as a non-root runtime user and verify volume permissions work with the actual image.
- Keep TLS and the reverse proxy outside this app. Include an example NGINX configuration in the README, including forwarding headers. Trust proxy headers only for the documented proxy arrangement; do not blindly trust arbitrary forwarded headers.
- This is a single-user app, not a multi-user authentication system. Require NGINX authentication, VPN, or another access-control layer before public exposure. State clearly that SSL alone does not restrict access; do not describe an unauthenticated public deployment as safe.
- Protect every state-changing browser request against CSRF, use POST for writes, escape rendered content, disable debug mode, and use secure session-cookie settings for HTTPS deployment.
- Read the session/CSRF secret from configuration. Provide instructions to generate one locally, never commit a real secret, and fail startup with a clear message if a required secret is absent. Do not place secrets in URLs or logs.
- Do not load fonts, scripts, or styles from third-party CDNs.

## 7. Tests and verification

Use test-driven development in small vertical slices: write one behavior test, run it and confirm the expected failure, implement the minimum to pass, rerun it, then refactor with tests passing. Do not write all implementation before adding tests.

Cover at least:

- IN then OUT creates exactly one completed interval.
- Duplicate IN, OUT without IN, double submissions, and concurrent IN requests cannot corrupt data.
- Multiple intervals, manual addition, editing, deleting, and manually closing an open interval.
- Invalid ordering, future timestamps, overlaps, conflicts with open intervals, and adjacent valid intervals.
- Empty months, leap-year February, multi-interval days, daily/monthly totals, and durations exceeding 24 hours.
- Overnight work, month-boundary work, daylight-saving transitions, and ambiguous/nonexistent manual local times.
- Open intervals are visible but excluded from finalized totals.
- CSV/print totals match screen totals if CSV is implemented.
- CSRF rejection, server-side validation, and persisted data after application restart.

Exercise the actual app and deployment before declaring completion:

1. Run the full automated suite and report its actual output summary.
2. Validate Compose configuration, build the image, and start the container using a throwaway test volume and test credentials/configuration.
3. Check health and load the home page; exercise IN/OUT, manual entry, and report through HTTP with real session and CSRF handling.
4. Restart/recreate the container without deleting its test volume, then verify recorded entries remain.
5. Check the phone-sized UI and print layout with a browser if available. If unavailable, explicitly report that visual checks remain unverified.
6. Remove only temporary test resources; never delete an existing user database or volume. If Docker is unavailable, say precisely which checks could not run rather than claiming deployment is verified.

## 8. Documentation and handoff

Write a README covering:

- Setup from a fresh checkout: configuration, timezone selection, secret generation, and Compose commands.
- URL/port, NGINX integration, required access control, and HTTPS cookie settings.
- Everyday IN/OUT usage, corrections, and monthly reporting.
- The rules for multiple intervals, breaks, incomplete entries, midnight/month boundaries, and daylight saving.
- Test commands, logs, health checks, and troubleshooting database permissions.
- Safe backup and restore: use SQLite's consistent backup facility, or stop the app before copying database files. Do not recommend copying only a live main database file when WAL may be active. Demonstrate/document restore without overwriting the original backup.
- Update/rebuild steps that preserve the data volume, and a warning that `docker compose down -v` deletes stored data.

Finish with a short report listing files delivered, commands actually run, checks that passed, and any remaining limitations. Do not fabricate test output. The acceptance criterion is a working, verified application meeting the original requirements, not merely a scaffold.
