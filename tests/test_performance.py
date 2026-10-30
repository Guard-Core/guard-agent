import asyncio
import gc
import os
import sys
import time
from datetime import datetime, timezone
from types import FrameType
from unittest.mock import AsyncMock, MagicMock, patch

import psutil
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from guard import SecurityConfig, SecurityMiddleware

import guard_agent as guard_agent_module
from guard_agent import SecurityEvent, guard_agent
from guard_agent.buffer import EventBuffer
from guard_agent.client import GuardAgentHandler
from guard_agent.models import AgentConfig
from guard_agent.transport import HTTPTransport


class TestPerformanceImpact:
    """Test that agent doesn't significantly impact performance."""

    def create_app_without_agent(self) -> FastAPI:
        """Create app without baseline."""
        app = FastAPI()
        config = SecurityConfig(
            rate_limit=1000,
            enable_agent=False,
            enable_redis=False,
            exclude_paths=["/test"],
            passive_mode=True,
            whitelist=("127.0.0.1",),
            whitelist_access_only=False,
            ban_on_hostile_signals=False,
        )
        app.add_middleware(SecurityMiddleware, config=config)

        @app.get("/test")
        def test_endpoint() -> dict[str, str]:
            return {"message": "test"}

        return app

    def create_app_with_agent(self) -> FastAPI:
        """Create app with agent enabled."""
        app = FastAPI()
        config = SecurityConfig(
            rate_limit=1000,
            enable_agent=True,
            enable_redis=False,
            agent_api_key="test-key-api-key-1",
            agent_project_id="test-project",
            exclude_paths=["/test"],
            passive_mode=True,
            ban_on_hostile_signals=False,
            whitelist_access_only=False,
        )
        app.add_middleware(SecurityMiddleware, config=config)

        @app.get("/test")
        def test_endpoint() -> dict[str, str]:
            return {"message": "test"}

        return app

    # The throughput floors below are loose sanity checks, best of N rounds.
    _PERF_ROUNDS = 5
    _PERF_REQUESTS_PER_ROUND = 100

    def _measure_best_of_n(self, app: FastAPI) -> tuple[float, float]:
        """Run the request loop N times and return (best_cpu_time, best_rps).

        Each round is timed in process CPU time, every thread included, so a
        loaded machine does not count as agent overhead. Best = fastest round.
        """
        client = TestClient(app)

        # Warmup
        for _ in range(10):
            client.get("/test")

        best_time = float("inf")
        for _ in range(self._PERF_ROUNDS):
            start_time = time.process_time()
            for _ in range(self._PERF_REQUESTS_PER_ROUND):
                response = client.get("/test")
                assert response.status_code == 200
            round_time = time.process_time() - start_time
            best_time = min(best_time, round_time)

        best_rps = self._PERF_REQUESTS_PER_ROUND / best_time
        return best_time, best_rps

    def _measure_baseline_performance(self) -> tuple[float, float]:
        """Measure baseline performance without agent (helper method)."""
        app = self.create_app_without_agent()
        baseline_time, baseline_rps = self._measure_best_of_n(app)
        print(f"Baseline: {baseline_rps:.1f} RPS (best of {self._PERF_ROUNDS})")
        return baseline_time, baseline_rps

    def test_baseline_performance(self) -> None:
        """Test baseline performance without agent."""
        baseline_time, baseline_rps = self._measure_baseline_performance()

        # Assert reasonable performance (should handle at least 100 RPS)
        assert baseline_rps > 100, (
            f"Baseline performance too slow: {baseline_rps:.1f} RPS"
        )
        assert baseline_time < 5.0, f"Baseline test took too long: {baseline_time:.2f}s"

    # The agent's overhead on the request path is counted in calls made from
    # guard_agent's own code, not timed: a wall or CPU clock moves with host
    # load (this test failed at 30.5% on a loaded host with no code change),
    # a call count does not. Detection is off in both apps so the baseline is
    # the plain request pipeline, and both apps share one config apart from
    # enable_agent. 10% of the baseline's calls is about twice the measured
    # share (5.6%), so an agent path that doubles in cost fails.
    _CALLS_REQUESTS = 20
    _CALLS_MAX_OVERHEAD = 0.10

    def _create_counted_app(self, enable_agent: bool) -> FastAPI:
        app = FastAPI()
        config = SecurityConfig(
            rate_limit=100000,
            enable_redis=False,
            passive_mode=True,
            enable_penetration_detection=False,
            ban_on_hostile_signals=False,
            whitelist_access_only=False,
            enable_agent=enable_agent,
            agent_api_key="test-api-key-0123456789" if enable_agent else None,
            agent_project_id="test-project" if enable_agent else None,
        )
        app.add_middleware(SecurityMiddleware, config=config)

        @app.get("/test")
        def test_endpoint() -> dict[str, str]:
            return {"message": "test"}

        return app

    def _count_request_path_calls(self, app: FastAPI) -> tuple[int, int]:
        """Return (all calls, calls made by guard_agent code) over the loop."""
        agent_dir = os.path.dirname(guard_agent_module.__file__)
        counts = {"all": 0, "agent": 0}

        def profiler(frame: FrameType, event: str, arg: object) -> None:
            if event in ("call", "c_call"):
                counts["all"] += 1
                if frame.f_code.co_filename.startswith(agent_dir):
                    counts["agent"] += 1

        async def start_counting() -> None:
            sys.setprofile(profiler)

        async def stop_counting() -> None:
            sys.setprofile(None)

        with TestClient(app) as client:
            assert client.portal is not None
            for _ in range(10):
                client.get("/test")
            counts["all"] = counts["agent"] = 0
            client.portal.call(start_counting)
            for _ in range(self._CALLS_REQUESTS):
                assert client.get("/test").status_code == 200
            client.portal.call(stop_counting)
        return counts["all"], counts["agent"]

    def test_agent_performance_impact(self) -> None:
        """The agent's request-path overhead stays a small share of the calls."""
        handler = GuardAgentHandler(
            AgentConfig(api_key="test-api-key-0123456789", buffer_size=10000)
        )
        handler.transport = AsyncMock()
        with (
            patch.object(handler, "start", AsyncMock()),
            patch("guard_agent.guard_agent", return_value=handler),
        ):
            _, agent_calls = self._count_request_path_calls(
                self._create_counted_app(enable_agent=True)
            )
        baseline_calls, baseline_agent_calls = self._count_request_path_calls(
            self._create_counted_app(enable_agent=False)
        )

        assert baseline_agent_calls == 0
        assert len(handler.buffer.metric_buffer) >= self._CALLS_REQUESTS, (
            "the agent never saw the requests, so nothing was measured"
        )
        overhead = agent_calls / baseline_calls
        print(f"Agent calls per request: {agent_calls / self._CALLS_REQUESTS:.1f}")
        print(f"Performance impact: {overhead * 100:.1f}%")
        assert overhead < self._CALLS_MAX_OVERHEAD, (
            f"Agent adds {overhead * 100:.1f}% calls to the request path "
            f"(threshold {self._CALLS_MAX_OVERHEAD * 100:.0f}%)"
        )

    @pytest.mark.asyncio
    async def test_buffer_performance(self) -> None:
        """Test buffer performance under load."""
        config = AgentConfig(api_key="test-api-key-1", buffer_size=1000)
        buffer = EventBuffer(config)

        # Mock Redis to avoid external dependencies
        buffer.redis_handler = AsyncMock()

        events = [
            SecurityEvent(
                timestamp=datetime.fromtimestamp(time.time(), tz=timezone.utc),
                event_type="ip_banned",
                ip_address=f"192.168.1.{i % 255}",
                action_taken="logged",
                reason="performance_test",
            )
            for i in range(1000)
        ]

        # Measure buffer add performance
        with patch.object(asyncio, "sleep", wraps=asyncio.sleep) as sleep_spy:
            start_time = time.process_time()
            for event in events:
                await buffer.add_event(event)
            add_time = time.process_time() - start_time
        sleep_spy.assert_not_called()

        events_per_second = 1000 / add_time

        print(f"Buffer performance: {events_per_second:.1f} events/sec")

        # Should handle at least 1000 events per second
        assert events_per_second > 1000, (
            f"Buffer too slow: {events_per_second:.1f} events/sec"
        )

    @pytest.mark.asyncio
    async def test_transport_performance(self) -> None:
        """Test transport performance under load."""
        config = AgentConfig(api_key="test-api-key-1", timeout=1)
        transport = HTTPTransport(config)

        # Mock HTTP client
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={"status": "ok"})

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()
        transport._client = mock_client

        events = [
            SecurityEvent(
                timestamp=datetime.fromtimestamp(time.time(), tz=timezone.utc),
                event_type="rate_limited",
                ip_address=f"192.168.1.{i % 255}",
                action_taken="logged",
                reason="performance_test",
            )
            for i in range(100)
        ]

        # Measure transport performance
        with patch.object(asyncio, "sleep", wraps=asyncio.sleep) as sleep_spy:
            start_time = time.process_time()
            for i in range(0, 100, 10):  # Send in batches of 10
                batch = events[i : i + 10]
                await transport.send_events(batch)
            transport_time = time.process_time() - start_time
        sleep_spy.assert_not_called()

        events_per_second = 100 / transport_time

        print(f"Transport performance: {events_per_second:.1f} events/sec")

        # Should handle at least 100 events per second
        assert events_per_second > 100, (
            f"Transport too slow: {events_per_second:.1f} events/sec"
        )

    def test_memory_usage(self) -> None:
        """Test memory usage doesn't grow excessively."""
        gc.collect()
        gc.collect()
        process = psutil.Process(os.getpid())
        initial_memory = process.memory_info().rss

        # Create app with agent
        app = self.create_app_with_agent()

        with patch("guard_agent.guard_agent") as mock_guard_agent:
            mock_agent = AsyncMock()
            mock_guard_agent.return_value = mock_agent

            client = TestClient(app)

            for _ in range(100):
                response = client.get("/test")
                assert response.status_code == 200

            final_memory = process.memory_info().rss
            memory_increase = final_memory - initial_memory
            memory_increase_mb = memory_increase / (1024 * 1024)

            print(f"Memory increase: {memory_increase_mb:.1f} MB")

            assert memory_increase_mb < 100, (
                f"Excessive memory usage: {memory_increase_mb:.1f} MB"
            )

    @pytest.mark.asyncio
    async def test_concurrent_load(self) -> None:
        """Test performance under concurrent load."""
        config = AgentConfig(api_key="test-api-key", buffer_size=100)
        agent = guard_agent(config)
        assert isinstance(agent, GuardAgentHandler)

        # Mock transport
        agent.transport = AsyncMock()
        agent.transport.send_events.return_value = True

        async def send_events_worker(worker_id: int) -> None:
            """Worker function to send events concurrently."""
            events = [
                SecurityEvent(
                    timestamp=datetime.fromtimestamp(time.time(), tz=timezone.utc),
                    event_type="suspicious_request",
                    ip_address=f"192.168.{worker_id}.{i}",
                    action_taken="logged",
                    reason="concurrent_load_test",
                )
                for i in range(10)
            ]

            for event in events:
                await agent.send_event(event)

        # Run multiple workers concurrently
        start_time = time.process_time()
        await asyncio.gather(*[send_events_worker(i) for i in range(10)])
        concurrent_time = time.process_time() - start_time
        total_events = 10 * 10  # 10 workers × 10 events
        events_per_second = total_events / concurrent_time

        print(f"Concurrent performance: {events_per_second:.1f} events/sec")

        # Should handle concurrent load efficiently
        assert events_per_second > 500, (
            f"Poor concurrent performance: {events_per_second:.1f} events/sec"
        )
