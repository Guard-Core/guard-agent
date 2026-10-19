from guard_agent.models import DynamicRules

RULES_STALE_WARNING_INTERVAL = 3600.0


def rules_are_stale(
    cached: DynamicRules | None, last_successful_check: float | None, now: float
) -> bool:
    """True when the SaaS has failed for longer than the cached rules' ttl."""
    if cached is None or last_successful_check is None:
        return False
    return now - last_successful_check > cached.ttl
