# Slim, pinned, non-root. Build context is the project directory.
FROM debian:bookworm-slim

ENV PYTHONUNBUFFERED=1 LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive

# Only what is needed: CPython + venv. Everything else is pinned from PyPI.
RUN apt-get update \
 && apt-get install -y python3 python3-venv \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt

# Pinned virtualenv: pip refuses to resolve anything not pinned in the file.
RUN python3 -m venv /app/.venv \
 && /app/.venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && /app/.venv/bin/pip freeze > /app/pip-freeze.txt \
 && grep -qx -- '^Flask==3\.1\.3$' /app/pip-freeze.txt \
 && grep -qx -- '^Werkzeug==3\.1\.9$' /app/pip-freeze.txt \
 && grep -qx -- '^gunicorn==26\.2\.0$' /app/pip-freeze.txt \
 && grep -qx -- '^tzdata==2026\.5$' /app/pip-freeze.txt

# Runtime user: never root, and /data is writable by it.
RUN useradd --uid 10001 --user-group --shell /usr/sbin/nologin timesheet \
 && install -d -m 0775 /data

COPY clock.py /app/clock.py
COPY db.py /app/db.py
COPY timeutil.py /app/timeutil.py
COPY report.py /app/report.py
COPY app.py /app/app.py
COPY wsgi.py /app/wsgi.py
COPY gunicorn.conf.py /app/gunicorn.conf.py
COPY templates /app/templates
COPY static /app/static
COPY tests /app/tests
COPY pytest.ini /app/pytest.ini

WORKDIR /app
USER timesheet
VOLUME ["/data"]
EXPOSE 8080

# Fixed worker count, no auto-reload (see gunicorn.conf.py).
CMD ["/app/.venv/bin/gunicorn", "-c", "/app/gunicorn.conf.py"]
