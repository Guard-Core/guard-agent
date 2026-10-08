# Flask Adapter — `flaskapi-guard`

Guard Agent integrates with Flask through the [`flaskapi-guard`](https://pypi.org/project/flaskapi-guard/) extension. Flask is synchronous, so the extension handles the agent's lifecycle internally — no `lifespan` or manual `start()`/`stop()` calls are required.

## Install

```bash
uv add flaskapi-guard guard-agent
```

Alternatives:

```bash
poetry add flaskapi-guard guard-agent
```

```bash
pip install flaskapi-guard guard-agent
```

## Minimal example

```python
import os

from flask import Flask
from flaskapi_guard import FlaskAPIGuard, SecurityConfig, SecurityDecorator

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

app = Flask(__name__)
guard_decorator = SecurityDecorator(security_config)
guard = FlaskAPIGuard(app, config=security_config)
guard.set_decorator_handler(guard_decorator)


@app.route("/")
def root() -> dict[str, str]:
    return {"message": "Hello, Guard!"}
```

## App factory pattern

For Flask apps using the factory pattern, use the deferred `init_app()`:

```python
from flask import Flask
from flaskapi_guard import FlaskAPIGuard, SecurityConfig, SecurityDecorator

guard = FlaskAPIGuard()


def create_app() -> Flask:
    app = Flask(__name__)
    security_config = SecurityConfig(
        enable_agent=True,
        agent_api_key="YOUR_API_KEY",
        agent_project_id="YOUR_PROJECT_ID",
        agent_endpoint="https://api.guard-core.com",
    )
    decorator = SecurityDecorator(security_config)
    guard.init_app(app, config=security_config)
    guard.set_decorator_handler(decorator)
    return app
```

## How the agent is wired

- `FlaskAPIGuard.__init__()` / `init_app()` read `config.enable_agent`. When `True`, they call `config.to_agent_config()` to auto-derive an `AgentConfig`, then call `guard_agent(agent_config)` internally to instantiate the singleton handler.
- The handler is stored at `guard.agent_handler` on the extension instance. Accessing it directly is rarely needed.
- Because Flask is synchronous, agent start/stop and event flushing run via `guard_core`'s sync-to-async bridge. No `lifespan` or manual lifecycle calls are required.

## Dashboard & Playground

- Obtain your API key and project ID at [**app.guard-core.com**](https://app.guard-core.com).
- Try the full stack without installing anything at [**playground.guard-core.com**](https://playground.guard-core.com).

## Related

- [`flaskapi-guard` on GitHub](https://github.com/Guard-Core/flaskapi-guard)
