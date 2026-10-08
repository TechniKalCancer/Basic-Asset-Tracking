"""FoxDesk — school device, loaner, repair and help-desk tracking.

Importing the package builds the Flask app and registers every page:
core (app/config/db) → models → services → integrations → web (template
helpers) → views. URLs and endpoint names are unchanged from the original
single-file app.py, so templates and url_for() calls didn't move.
"""
from foxdesk.core import app, db  # noqa: F401
from foxdesk import models  # noqa: F401
from foxdesk.services import features  # noqa: F401  (registers the switched-off-module guard)
from foxdesk import web  # noqa: F401
from foxdesk.services import scheduler  # noqa: F401  (starts the background loops when configured)
from foxdesk import views  # noqa: F401
