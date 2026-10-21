"""Integration smoke tests for the Tornado adapter (tornadoapi-guard).

These tests verify that guard-agent integrates correctly with
``tornadoapi-guard`` when ``SecurityConfig.enable_agent=True``. They do not
exercise agent wire behavior (events, metrics, dynamic rules), which the unit
tests in ``test_client.py`` etc. cover. The contract is narrow:

1. ``SecurityConfig.to_agent_config()`` produces a valid ``AgentConfig``.
2. The adapter's middleware accepts an agent-enabled config without raising.
3. A request through the handler completes successfully.
"""

from typing import Any

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application
from tornadoapi_guard import SecurityConfig, SecurityHandler, SecurityMiddleware

from guard_agent.models import AgentConfig

API_KEY = "test-api-key-1234567890"
PROJECT_ID = "test-project"


class _RootHandler(SecurityHandler):
    def get(self) -> None:
        self.write({"ok": True})


class TestTornadoAdapterIntegration(AsyncHTTPTestCase):
    """Agent wiring via tornadoapi-guard (Tornado adapter)."""

    def get_app(self) -> Application:
        config = SecurityConfig(
            enable_agent=True,
            enable_redis=False,
            agent_api_key=API_KEY,
            agent_project_id=PROJECT_ID,
            passive_mode=True,
            ban_on_hostile_signals=False,
            rate_limit=100,
        )
        settings: dict[str, Any] = {
            "security_middleware": SecurityMiddleware(config=config)
        }
        return Application([(r"/", _RootHandler)], **settings)

    def test_security_config_produces_agent_config(self) -> None:
        """`SecurityConfig.to_agent_config()` returns a valid `AgentConfig`."""
        config = SecurityConfig(
            enable_agent=True,
            enable_redis=False,
            agent_api_key=API_KEY,
            agent_project_id=PROJECT_ID,
            ban_on_hostile_signals=False,
            rate_limit=100,
        )

        agent_config = config.to_agent_config()

        assert agent_config is not None
        assert isinstance(agent_config, AgentConfig)
        assert agent_config.api_key == API_KEY
        assert agent_config.project_id == PROJECT_ID

    def test_app_with_agent_enabled_serves_requests(self) -> None:
        """The handler attaches cleanly and requests flow through."""
        response = self.fetch("/")

        middleware = self._app.settings["security_middleware"]
        assert middleware.agent_handler is not None
        assert response.code == 200
        assert response.body == b'{"ok": true}'
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_agent_disabled_does_not_produce_config(self) -> None:
        """With `enable_agent=False`, `to_agent_config()` returns `None`."""
        config = SecurityConfig(
            enable_agent=False,
            enable_redis=False,
            ban_on_hostile_signals=False,
            rate_limit=100,
        )

        assert config.to_agent_config() is None
