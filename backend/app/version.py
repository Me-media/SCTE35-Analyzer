"""Single source of truth for the GUI app's own version number (distinct
from app.core.probe's __version__, which tracks the forked-in CLI engine).
Bump this alongside an entry in CHANGELOG.md at the repo root.

Exposed to the frontend via GET /api/version (see app/api/routes.py) so
the header can show it -- the simplest way to confirm a deploy actually
picked up new code instead of the browser serving a stale cached build.
"""
__version__ = "0.10.0"
