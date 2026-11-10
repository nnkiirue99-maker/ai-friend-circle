"""WSGI factories. Environment is read only when a factory is explicitly called."""

import os
from pathlib import Path
from .app import create_app
from .config import Config
from .gateway import create_gateway
from .mcp_server import create_mcp
from .service import Federation


def configured_service():
    return Federation(Config(os.environ["FC_ORIGIN"], Path(os.environ["FC_STATE"])))


def public_app():
    app = create_app(configured_service())
    if os.environ.get("FC_TRUSTED_PROXY") == "1":
        # Only for a loopback-bound listener behind the operator's own reverse proxy, which
        # must append the real peer to X-Forwarded-For. Used solely for per-address rate
        # limits; the canonical origin and Host still come from configuration, never headers.
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=0, x_host=0, x_port=0, x_prefix=0)
    return app


def private_app():
    return create_gateway(
        configured_service(),
        token_path=os.environ["FC_GATEWAY_TOKEN_FILE"],
        ui_origin=os.environ["FC_UI_ORIGIN"],
    )


def mcp_app():
    """AI-only MCP endpoint. Bind to loopback or a Unix socket; never proxy it publicly."""
    return create_mcp(configured_service())
