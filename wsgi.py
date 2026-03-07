"""
WSGI entry point for Virtual Guard.
"""

import os
import sys
import threading
import time

# Add both the file's directory AND virtual_guard subdir to path
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)

# Also try virtual_guard subfolder in case repo was uploaded with that structure
_subdir = os.path.join(_here, 'virtual_guard')
if os.path.isdir(_subdir):
    sys.path.insert(0, _subdir)

from app.main import app, sessions

application = app


def _cleanup_loop():
    while True:
        time.sleep(300)
        sessions.cleanup_expired()


_t = threading.Thread(target=_cleanup_loop, daemon=True)
_t.start()


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    port  = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=debug)
