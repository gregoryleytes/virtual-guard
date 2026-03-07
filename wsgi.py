"""
WSGI entry point for Virtual Guard.
Run locally: python wsgi.py
Production:  gunicorn wsgi:application -w 4 -b 0.0.0.0:8000
"""

import os
import threading
import time
from app.main import app, sessions

application = app   # gunicorn looks for `application`


def _cleanup_loop():
    while True:
        time.sleep(300)  # every 5 minutes
        sessions.cleanup_expired()


# Start background cleanup thread
_t = threading.Thread(target=_cleanup_loop, daemon=True)
_t.start()


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    port  = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=debug)
