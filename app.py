"""Entry point kept at the project root so nothing that runs the app has to
change: gunicorn `app:app`, FLASK_APP=app.py (Flask-Migrate), and
`python app.py` for local development. The application itself lives in the
foxdesk/ package."""
from foxdesk import app, db  # noqa: F401
from foxdesk.core import DEBUG_MODE

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081, debug=DEBUG_MODE)
