# Timesheet — personal work-hours tracker

One screen to punch **IN / OUT** with the server clock, one screen to add or
correct times manually, one screen for a monthly report with day totals, a
downloadable CSV and a clean print layout.

Data is a single SQLite file. The app is single-user: no accounts, no login.

## Screens

| Path | What it does |
| --- | --- |
| `/` | Status ("At work since …" / "Not at work"), big IN and OUT buttons, today's recorded hours, recent entries |
| `/times` | Add a completed interval, add a missing IN, close an open interval, correct an interval, delete an accidental one |
| `/report` | Month report (`/report?month=YYYY-MM`, defaults to the current month) |
| `/report/2024-06.csv` | CSV download for that month |
| `/times/entry/3` | One entry, clicked from either interval list: shows it and offers **Edit** (form pre-filled with the recorded values) and **Delete** (needs the DELETE confirmation) |
| `/health` | `{"status": "ok"}` — for CI/production checks |

IN and OUT are disabled in the page when they do not apply, and the server
rejects them again on its own, so a stale page or a double tap cannot create a
second interval.

## Configuration (`.env`)

Copy `.env.example` to `.env`:

| Variable | Meaning |
| --- | --- |
| `APP_TIMEZONE` | IANA timezone your hours belong to (required, validated at startup) |
| `SECRET_KEY` | Session/CSRF secret (required; startup fails with a clear message if absent) |
| `DATABASE_PATH` | SQLite path inside the container, default `/data/timesheet.sqlite3` |
| `BIND` | Gunicorn bind address, default `0.0.0.0:8080` |
| `COOKIE_SECURE` | Send the session cookie only over HTTPS (default `1`) |
| `TRUST_PROXY` | Trust `X-Forwarded-Proto` (default `0`, see **Reverse proxy**) |

Generate the secret once per install:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## Docker

```sh
cp .env.example .env            # then edit SECRET_KEY
install -d -m 0775 data         # host directory bind-mounted to /data
docker compose build
docker compose up -d
```

Open <http://localhost:10801>. The database file
`data/timesheet.sqlite3` is created on the first run; nothing else is stored.

### Bind mount vs volume

`compose.yaml` shows three options; only one is active at a time.

1. **Directory bind mount** (active): `./data:/data`. Prepare the host side so
   the runtime user (uid `10001`) can write the file:
   ```sh
   install -d -m 0775 data
   install -m 0664 /dev/null data/timesheet.sqlite3   # optional, app can create it
   chown 10001 data/timesheet.sqlite3                 # if you created it as root
   ```
   A bind-mounted *file* keeps its host owner, so the app would otherwise fail
   with a "cannot open the database" message at startup.
2. **Single-file bind mount** (commented): `./data/timesheet.sqlite3:/data/timesheet.sqlite3`.
   Uncomment only once the file exists — Docker creates a *directory* when the
   path is missing, and SQLite then refuses to start.
3. **Named volume** (commented): `timesheet-data:/data`. Easiest first run;
   Docker owns the directory, so `chown` it to uid 10001 through a helper bind
   mount if you want to read it from the host later.

### Permissions

The image runs as `timesheet` (uid `10001`), never root. `/data` is mode `0775`
so the group can write. For bind mounts, make the file `0664` and owned by
`10001` (or group-writable) — see the commands above.

## Backup

The database is one file, so backup is a copy — and SQLite must not be copied
while a write is in flight. Writes are short, so either stop the service or use
the safe helper:

```sh
docker compose stop timesheet
docker compose cp timesheet:/data/timesheet.sqlite3 ./backups/timesheet-$(date +%F).sqlite3
docker compose up -d
```

Preferred (crash-safe, uses SQLite's own snapshot writer — stdlib sqlite3
`Connection.backup`, available in Python 3.11+, no manual byte reading):

```sh
# 1. write a consistent snapshot *inside* the data directory (SQLite's own
#    snapshot writer — safe even if a write is in flight)
docker compose exec timesheet /app/.venv/bin/python - <<'PY'
import sqlite3
src = sqlite3.connect("/data/timesheet.sqlite3")
dst = sqlite3.connect("/data/backup.sqlite3")
src.backup(dst)
dst.close(); src.close()
PY

# 2. the snapshot is already visible on the host through the ./data bind mount
mv data/backup.sqlite3 backups/timesheet-$(date +%F).sqlite3
```

With the named-volume option, use `docker compose cp timesheet:/data/backup.sqlite3 ./backups/…`
instead of step 2 (or `docker cp <image> …` once the image is tagged).

If the SQLite CLI is installed, the equivalent dot command is:

```sh
sqlite3 /data/timesheet.sqlite3 ".backup /tmp/timesheet-backup.sqlite3"
```

A plain file copy (`docker compose cp`, `cp`, `rsync`) is also fine **while no
write is running** — writes here are a few milliseconds, so stopping the
service first is enough.

Restore by moving the file back to `data/timesheet.sqlite3` (same ownership,
`0664`) and starting the service.

## Timezone and DST

`APP_TIMEZONE` is the only timezone setting. Punches use the **server clock**,
so they cannot be wrong; only manual entries are typed as local times.

* A local time that does not exist (clocks jumped forward, e.g. `02:00`–`03:00`
  on 2024-03-31 in Europe/Rome) is **rejected** with an explanation.
* A local time that happens twice (clocks went back, e.g. `02:00`–`03:00` on
  2024-10-27 in Europe/Rome) is **rejected until you pick an offset** — the
  form has an "offset" field for exactly that.
* Overnight work is unambiguous: the manual form asks for the **end date**
  explicitly, and the report splits an interval across the local days it spans.
* Totals are sums of intervals, never midnight-to-midnight spans.

## CSRF and double submission

* Each browser session gets a CSRF token; every form must echo it back, otherwise
  the request is rejected with HTTP 400 and nothing is written.
* Each rendered form also carries an idempotency token. Replaying that exact
  form (double tap, browser retry, bookmark) is recognised by the database and
  does nothing new — the page shows the same "Saved: …" message again.
* A deliberate new action comes from a freshly rendered page and gets a new
  token, so it is processed normally.

## Reverse proxy

`TRUST_PROXY` stays `0` by default: Flask does **not** guess the original scheme.
If you put NGINX in front and terminate TLS there, document the arrangement and
then set `TRUST_PROXY=1`, e.g.

```nginx
location / {
  proxy_pass http://127.0.0.1:10801;   # host port from compose.yaml (10801:8080)
  X-Forwarded-Proto $scheme;           # Flask reads this only when TRUST_PROXY=1
}
```

Keep `COOKIE_SECURE=1` for HTTPS. Do not enable proxy trust for any other reason.

## Development (no Docker)

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
APP_TIMEZONE=Europe/Rome SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') \
DATABASE_PATH=./data/timesheet.sqlite3 COOKIE_SECURE=0 \
.venv/bin/gunicorn -c gunicorn.conf.py
```

Tests (TDD slices, all in `tests/`):

```sh
.venv/bin/pytest -v
```

## CI / production verification

```sh
curl -fsS localhost:10801/health          # {"status":"ok"}
docker compose exec timesheet /app/.venv/bin/pytest -q -p no:cacheprovider   # full suite inside the image
```

`/health` returns 200 only when the app started, i.e. the secret, the timezone
and the database are all usable.

Startup is fail-fast (`preload_app = True`): a missing `SECRET_KEY`, a bad
`APP_TIMEZONE`, or an unwritable `/data` aborts immediately with one clear line
on stderr and exit code 1 — no silent half-started service, no retry spam.

## Implementation notes

* Python 3.12 stdlib `zoneinfo` + pinned `tzdata`; no `pytz`, no `pendulum`.
* SQLite: one open interval (unique index over open rows), `CHECK(end > start)`,
  adjacency allowed, overlaps rejected.
* Writes are one transaction each, serialised by an in-process lock plus SQLite's
  busy timeout and a bounded retry, so two processes cannot both open an interval.
* Schema changes go through `SQL_MIGRATIONS` in `db.py`; the schema version is
  stored in `meta`.
* Pinned dependency versions are in `requirements.txt` and re-checked at build
  time in `Dockerfile`.
