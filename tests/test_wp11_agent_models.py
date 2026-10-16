from guard_agent.models import DynamicRules


def test_dynamic_rules_accepts_is_platform_default_and_unknown_fields() -> None:
    rules = DynamicRules(
        rule_id="default-p", version=1, is_platform_default=True, brand_new_field=1
    )
    assert rules.is_platform_default is True
    assert DynamicRules(rule_id="r").is_platform_default is False


def test_dynamic_rules_drops_emergency_whitelist_only() -> None:
    assert "emergency_whitelist_only" not in DynamicRules.model_fields
    rules = DynamicRules(rule_id="r", emergency_whitelist_only=True)
    assert not hasattr(rules, "emergency_whitelist_only")
