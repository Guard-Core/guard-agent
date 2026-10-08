# Django Adapter — `djapi-guard`

Guard Agent integrates with Django through the [`djapi-guard`](https://pypi.org/project/djapi-guard/) middleware. The PyPI distribution is `djapi-guard`; the Python import path is `djangoapi_guard`. The middleware handles agent wiring internally — no custom `AppConfig.ready()` hook is required.

## Install

```bash
uv add djapi-guard guard-agent
```

Alternatives:

```bash
poetry add djapi-guard guard-agent
```

```bash
pip install djapi-guard guard-agent
```

## Minimal example

Configure both the middleware and the shared `SecurityConfig` in your Django settings:

```python
# settings.py
import os

from guard_core.models import SecurityConfig

MIDDLEWARE = [
    "djangoapi_guard.middleware.DjangoAPIGuard",
    "django.middleware.common.CommonMiddleware",
    # ... your existing middleware ...
]

api_key = os.environ.get("GUARD_API_KEY", "")
project_id = os.environ.get("GUARD_PROJECT_ID", "")
core_url = os.environ.get("GUARD_CORE_URL", "https://api.guard-core.com")

GUARD_SECURITY_CONFIG = SecurityConfig(
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
```

```python
# views.py
from django.http import JsonResponse


def root(request):
    return JsonResponse({"message": "Hello, Guard!"})
```

```python
# urls.py
from django.urls import path

from . import views

urlpatterns = [path("", views.root)]
```

## How the agent is wired

- `DjangoAPIGuard.__init__()` reads `settings.GUARD_SECURITY_CONFIG` during Django's middleware-loading phase.
- When `config.enable_agent=True`, it calls `config.to_agent_config()` to auto-derive an `AgentConfig`, then `guard_agent(agent_config)` to instantiate the singleton handler. The handler is stored at `middleware.agent_handler`.
- Because Django's request handling is synchronous by default, the agent's async methods (flush, send_event) run via `guard_core`'s sync-to-async bridge. No manual `start()`/`stop()` calls are required.
- If your Django project is running under ASGI (e.g. with Daphne or Uvicorn + async views), the same middleware works — the bridge detects the event loop and dispatches natively when possible.

## Dashboard & Playground

- Obtain your API key and project ID at [**app.guard-core.com**](https://app.guard-core.com).
- Try the full stack without installing anything at [**playground.guard-core.com**](https://playground.guard-core.com).

## Related

- [`djapi-guard` on GitHub](https://github.com/Guard-Core/djangoapi-guard)
