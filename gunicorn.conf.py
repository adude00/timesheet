"""Gunicorn configuration: fixed worker count, no auto-reload."""
import os

wsgi_app = "wsgi:application"
workers = 1  # one process: the write lock and the punch clock are per-process
threads = 4
reload = False  # never restart on file changes: a restart must not lose state
preload_app = True  # import the app in the master: a bad config aborts before forking
timeout = 30
graceful_timeout = 5
bind = os.environ.get("BIND", "0.0.0.0:8080")
accesslog = "-"
errorlog = "-"
loglevel = "info"
proc_name = "timesheet"
control_socket = "/tmp/gunicorn.ctl"  # $HOME is not writable by the runtime user
