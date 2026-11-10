"""Explicit host integration example. Importing this file does not initialize anything.

Call only after reviewing the host's authentication and approving deployment.
Do not import production app.py in tests. Do not use its chat/memory database.
"""

from pathlib import Path
from friend_circle.config import Config
from friend_circle.local_api import make_blueprint
from friend_circle.service import Federation


def install(app, *, authenticated_actor, federation_origin, state_directory, ui_origin):
    """authenticated_actor() MUST validate the host login and return a local Person ID.

    For the host app's current single-user session, the host can map a verified
    session['unlocked'] to its pre-created Person Actor. Never infer login from
    arbitrary request headers. Multi-user hosts must map each session separately.
    """
    service = Federation(Config(federation_origin, Path(state_directory)))
    app.register_blueprint(make_blueprint(service, authenticated_actor, ui_origin))
    return service
