# Tornado Adapter — `tornadoapi-guard`

Guard Agent integrates with Tornado through the [`tornadoapi-guard`](https://github.com/Guard-Core/tornadoapi-guard) middleware. Because Tornado is fully async, the agent's lifecycle is driven by `await security_middleware.initialize()` (starts the agent) and `await security_middleware.reset()` (stops it).

!!! warning "Adapter not yet on PyPI"
    `tornadoapi-guard` 1.0.0 has not been published to PyPI at the time of writing. Install from source during the pre-release window, or wait for the first published release.

## Install (from source, pre-release)

```bash
uv pip install "git+https://github.com/Guard-Core/tornadoapi-guard.git"
uv add guard-agent
```

Alternatives:

```bash
poetry add "git+https://github.com/Guard-Core/tornadoapi-guard.git"
poetry add guard-agent
```

```bash
pip install "git+https://github.com/Guard-Core/tornadoapi-guard.git"
pip install guard-agent
```

Once the adapter is published, use the plain form:

```bash
uv add tornadoapi-guard guard-agent
```

## Minimal example

```python
import asyncio
import os

import tornado.httpserver
import tornado.web
from tornadoapi_guard import SecurityConfig, SecurityDecorator, SecurityMiddleware


class RootHandler(tornado.web.RequestHandler):
    async def get(self) -> None:
        self.write({"message": "Hello, Guard!"})


def build_application(
    middleware: SecurityMiddleware, decorator: SecurityDecorator
) -> tornado.web.Application:
    return tornado.web.Application(
        [(r"/", RootHandler)],
        security_middleware=middleware,
        guard_decorator=decorator,
    )


async def main() -> None:
    api_key = os.environ.get("GUARD_API_KEY", "")
    project_id = os.environ.get("GUARD_PROJECT_ID", "")
    core_url = os.environ.get("GUARD_CORE_URL", "https://api.guard-core.com")

    security_config = SecurityConfig(
        # Security settings
        auto_ban_threshold=5,
        auto_ban_duration=300,
        enable_rate_limiting=True,
        rate_limit=100,
        rate_limit_window=60,

        # Agent telemetry
        enable_agent=bool(api_key),
        agent_api_key=api_key,
        agent_endpoint=core_url,
        agent_project_id=project_id,
    )

    security_middleware = SecurityMiddleware(config=security_config)
    guard_decorator = SecurityDecorator(security_config)
    security_middleware.set_decorator_handler(guard_decorator)

    # initialize() starts the agent (and everything else) on the current event loop.
    await security_middleware.initialize()

    app = build_application(security_middleware, guard_decorator)
    server = tornado.httpserver.HTTPServer(app)
    server.listen(8000, address="127.0.0.1")

    try:
        await asyncio.Event().wait()  # Run forever
    finally:
        await security_middleware.reset()


if __name__ == "__main__":
    asyncio.run(main())
```

## How the agent is wired

- `SecurityMiddleware(config=config)` reads `config.enable_agent`. When `True`, it calls `config.to_agent_config()` to auto-derive an `AgentConfig`, then `guard_agent(agent_config)` internally to instantiate the singleton handler (stored at `middleware.agent_handler`).
- **`await middleware.initialize()`** is mandatory — it starts the agent's background flush loop on the current event loop. Without it, events buffer but never flush.
- **`await middleware.reset()`** on shutdown stops the agent cleanly and ensures any buffered events are flushed before exit.
- The middleware is passed to `tornado.web.Application` via the `security_middleware` kwarg; the decorator goes via `guard_decorator`.

## Dashboard & Playground

- Obtain your API key and project ID at [**app.guard-core.com**](https://app.guard-core.com).
- Try the full stack without installing anything at [**playground.guard-core.com**](https://playground.guard-core.com).

## Related

- [`tornadoapi-guard` on GitHub](https://github.com/Guard-Core/tornadoapi-guard)
