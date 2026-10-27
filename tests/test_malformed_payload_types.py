import pytest

from guard_agent.models import AgentConfig
from guard_agent.transport import HTTPTransport
from guard_agent.utils import _model_like_to_dict


@pytest.fixture
def transport(agent_config: AgentConfig) -> HTTPTransport:
    return HTTPTransport(agent_config)


def test_non_iterable_events_raise_type_error(transport: HTTPTransport) -> None:
    with pytest.raises(TypeError, match="iterable of events or metrics, got int"):
        transport._redact_sensitive_headers({"events": 5})


def test_non_iterable_metrics_raise_type_error(transport: HTTPTransport) -> None:
    with pytest.raises(TypeError, match="iterable of events or metrics, got int"):
        transport._redact_sensitive_headers({"metrics": 5})


def test_non_object_event_raises_type_error(transport: HTTPTransport) -> None:
    with pytest.raises(TypeError, match="event object, got str"):
        transport._redact_sensitive_headers({"events": ["not-an-object"]})


def test_non_object_metric_raises_type_error(transport: HTTPTransport) -> None:
    with pytest.raises(TypeError, match="metric object, got str"):
        transport._redact_sensitive_headers({"metrics": ["not-an-object"]})


def test_value_without_dataclass_shape_or_model_dump_raises_type_error() -> None:
    with pytest.raises(TypeError, match="dataclass or a model_dump value, got object"):
        _model_like_to_dict(object())
