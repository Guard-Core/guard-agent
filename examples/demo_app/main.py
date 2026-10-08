import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from guard_agent import (
    AgentConfig,
    GuardAgentHandler,
    SecurityEvent,
    get_current_timestamp,
)

AGENT_ENDPOINT = os.getenv("GUARD_AGENT_ENDPOINT", "https://api.guard-core.com")
AGENT_API_KEY = os.getenv("GUARD_AGENT_API_KEY", "demo-api-key-12345")
AGENT_PROJECT_ID = os.getenv("GUARD_AGENT_PROJECT_ID", "demo-project")


def build_test_event() -> SecurityEvent:
    return SecurityEvent(
        timestamp=get_current_timestamp(),
        event_type="custom_request_check",
        ip_address="192.168.1.100",
        action_taken="logged",
        reason="Guard Agent demo container test event",
        endpoint="/demo/test-event",
        method="POST",
        metadata={"source": "guard-agent-demo-container"},
    )


agent_config = AgentConfig(
    api_key=AGENT_API_KEY,
    project_id=AGENT_PROJECT_ID,
    endpoint=AGENT_ENDPOINT,
    buffer_size=10,
    flush_interval=5,
)
agent = GuardAgentHandler(agent_config)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await agent.start()
    await agent.send_event(build_test_event())
    yield
    await agent.stop()


app = FastAPI(title="Guard Agent Demo", lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "guard-agent-demo",
        "endpoint": AGENT_ENDPOINT,
        "project_id": AGENT_PROJECT_ID,
    }


@app.get("/health")
async def health() -> dict[str, str]:
    status = await agent.get_status()
    return {"status": status.status}


@app.post("/events")
async def emit_test_event() -> dict[str, str]:
    event = build_test_event()
    await agent.send_event(event)
    return {"emitted": event.event_type}
