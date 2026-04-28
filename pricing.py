def calculate_price(result_data: dict, rate_low=35.0, rate_high=40.0, rate_premium=55.0, min_charge=75.0, market_rates=None) -> tuple:
    job_type = result_data.get("job_type", "standard")
    totals = result_data.get("totals", {})
    conditions = result_data.get("conditions", [])
    items = result_data.get("items", [])

    cy_mid = float(totals.get("cubic_yards_mid", totals.get("cubic_yards_low", 2.0)))
    cy_low = float(totals.get("cubic_yards_low", cy_mid * 0.9))
    cy_high = float(totals.get("cubic_yards_high", cy_mid * 1.1))

    is_premium = (
        job_type in ("premium", "hoarder", "truck_load")
        or "stairs" in conditions
        or "heavy_items" in conditions
        or "hoarder" in conditions
        or cy_mid > 10
    )

    if market_rates and market_rates.get("source") == "live_market_search":
        mkt_low = market_rates.get("low", rate_low)
        mkt_high = market_rates.get("high", rate_high)
        mkt_premium = market_rates.get("premium", rate_premium)
        eff_low = max(rate_low, mkt_low)
        eff_high = max(rate_high, mkt_high)
        eff_premium = max(rate_premium, mkt_premium)
    else:
        eff_low = rate_low
        eff_high = rate_high
        eff_premium = rate_premium

    if is_premium:
        r_low = eff_premium
        r_high = eff_premium
    else:
        r_low = eff_low
        r_high = eff_high

    price_low = cy_low * r_low
    price_high = cy_high * r_high

    min_charge_applied = price_low < min_charge or price_high < min_charge
    price_low = max(price_low, min_charge)
    price_high = max(price_high, min_charge)

    if price_high < price_low:
        price_high = round(price_low * 1.15, 2)

    special_items = [
        {"name": item.get("name", "Unknown"), "quantity": int(item.get("quantity", 1))}
        for item in items if item.get("is_special")
    ]

    return round(price_low, 2), round(price_high, 2), round(cy_mid, 1), special_items, min_charge_applied
