from guard_agent.models import DynamicRules


def test_dynamic_rules_accepts_is_platform_default_and_unknown_fields() -> None:
    rules = DynamicRules(
        rule_id="default-p", version=1, is_platform_default=True, brand_new_field=1
    )
    assert rules.is_platform_default is True
    assert DynamicRules(rule_id="r").is_platform_default is False


def test_dynamic_rules_keeps_emergency_whitelist_only() -> None:
    assert DynamicRules(rule_id="r").emergency_whitelist_only is True
    rules = DynamicRules.model_validate(
        {"rule_id": "r", "emergency_mode": True, "emergency_whitelist_only": False}
    )
    assert rules.emergency_whitelist_only is False
    assert rules.model_dump()["emergency_whitelist_only"] is False
