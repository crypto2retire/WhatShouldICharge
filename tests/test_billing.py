from billing import check_usage_limit, record_usage, reset_billing_cycle_if_needed, PLAN_CALL_LIMITS
from datetime import datetime, timezone


def _recent_start():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MockUser:
    def __init__(self, **kwargs):
        self.subscription_tier = kwargs.get("subscription_tier", "free")
        self.monthly_call_limit = kwargs.get("monthly_call_limit", None)
        self.monthly_calls_used = kwargs.get("monthly_calls_used", 0)
        self.billing_cycle_start = kwargs.get("billing_cycle_start", _recent_start())
        self.overage_mode = kwargs.get("overage_mode", "warn_and_charge")
        self.overage_cap_cents = kwargs.get("overage_cap_cents", 0)
        self.overage_charges_cents = kwargs.get("overage_charges_cents", 0)


class TestCheckUsageLimit:
    def test_under_limit_allowed(self):
        user = MockUser(subscription_tier="free", monthly_calls_used=1)
        allowed, err = check_usage_limit(user)
        assert allowed is True
        assert err is None

    def test_at_limit_warn_mode(self):
        user = MockUser(subscription_tier="free", monthly_calls_used=3)
        allowed, err = check_usage_limit(user)
        assert allowed is True

    def test_at_limit_hard_stop(self):
        user = MockUser(subscription_tier="free", monthly_calls_used=3, overage_mode="hard_stop")
        allowed, err = check_usage_limit(user)
        assert allowed is False
        assert err["detail"] == "monthly_limit_reached"

    def test_overage_cap_reached(self):
        user = MockUser(
            subscription_tier="solo",
            monthly_call_limit=5,
            monthly_calls_used=5,
            overage_mode="capped",
            overage_cap_cents=100,
            overage_charges_cents=100,
        )
        allowed, err = check_usage_limit(user)
        assert allowed is False
        assert err["detail"] == "overage_cap_reached"

    def test_overage_cap_not_reached(self):
        user = MockUser(
            subscription_tier="solo",
            monthly_call_limit=5,
            monthly_calls_used=5,
            overage_mode="capped",
            overage_cap_cents=500,
            overage_charges_cents=100,
        )
        allowed, err = check_usage_limit(user)
        assert allowed is True

    def test_solo_limit_150(self):
        assert PLAN_CALL_LIMITS["solo"] == 150

    def test_enterprise_limit_2500(self):
        assert PLAN_CALL_LIMITS["enterprise"] == 2500

    def test_custom_monthly_limit_overrides_tier(self):
        user = MockUser(subscription_tier="free", monthly_call_limit=100, monthly_calls_used=50)
        allowed, err = check_usage_limit(user)
        assert allowed is True


class TestRecordUsage:
    def test_increments_counter(self):
        user = MockUser(monthly_calls_used=5)
        record_usage(user)
        assert user.monthly_calls_used == 6

    def test_overage_charge_applied(self):
        user = MockUser(subscription_tier="solo", monthly_call_limit=10, monthly_calls_used=10)
        record_usage(user)
        assert user.monthly_calls_used == 11
        assert user.overage_charges_cents == 10

    def test_no_overage_under_limit(self):
        user = MockUser(subscription_tier="solo", monthly_call_limit=10, monthly_calls_used=5)
        record_usage(user)
        assert user.overage_charges_cents == 0


class TestResetBillingCycle:
    def test_resets_when_null(self):
        user = MockUser(billing_cycle_start=None, monthly_calls_used=50, overage_charges_cents=200)
        result = reset_billing_cycle_if_needed(user)
        assert result is True
        assert user.monthly_calls_used == 0
        assert user.overage_charges_cents == 0
        assert user.billing_cycle_start is not None

    def test_no_reset_when_recent(self):
        user = MockUser(billing_cycle_start=_recent_start(), monthly_calls_used=50)
        result = reset_billing_cycle_if_needed(user)
        assert result is False
        assert user.monthly_calls_used == 50
