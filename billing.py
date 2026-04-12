from datetime import datetime, timezone

PLAN_CALL_LIMITS = {"free": 3, "solo": 150, "team": 750, "enterprise": 2500, "custom": 999}
OVERAGE_RATE_CENTS = {"solo": 10, "team": 10, "enterprise": 8, "custom": 10}


def reset_billing_cycle_if_needed(user):
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    if user.billing_cycle_start is None or (today - user.billing_cycle_start).days >= 30:
        user.monthly_calls_used = 0
        user.overage_charges_cents = 0
        user.billing_cycle_start = today
        return True
    return False


def check_usage_limit(user):
    reset_billing_cycle_if_needed(user)
    limit = user.monthly_call_limit or PLAN_CALL_LIMITS.get(user.subscription_tier, 3)
    used = user.monthly_calls_used or 0

    if used < limit:
        return True, None

    mode = getattr(user, 'overage_mode', 'warn_and_charge') or 'warn_and_charge'

    if mode == 'hard_stop':
        return False, {
            "detail": "monthly_limit_reached",
            "message": "You've reached your monthly estimate limit. An owner or manager can add more funds or change your overage settings.",
            "used": used, "limit": limit
        }

    if mode == 'capped':
        cap = getattr(user, 'overage_cap_cents', 0) or 0
        charged = getattr(user, 'overage_charges_cents', 0) or 0
        if charged >= cap:
            return False, {
                "detail": "overage_cap_reached",
                "message": f"You've reached your ${cap/100:.2f} overage cap. An owner or manager can increase the cap or add funds.",
                "used": used, "limit": limit, "overage_spent": charged
            }

    return True, None


def record_usage(user):
    user.monthly_calls_used = (user.monthly_calls_used or 0) + 1
    limit = user.monthly_call_limit or PLAN_CALL_LIMITS.get(user.subscription_tier, 3)
    if user.monthly_calls_used > limit:
        rate = OVERAGE_RATE_CENTS.get(user.subscription_tier, 10)
        user.overage_charges_cents = (user.overage_charges_cents or 0) + rate
