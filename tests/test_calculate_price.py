from pricing import calculate_price


class TestCalculatePriceBasic:
    def test_standard_job(self):
        price_low, price_high, cy_mid, special_items, min_applied = calculate_price(
            {
                "job_type": "standard",
                "totals": {"cubic_yards_mid": 4.0, "cubic_yards_low": 3.5, "cubic_yards_high": 4.5},
                "conditions": [],
                "items": [],
            },
            rate_low=35.0,
            rate_high=40.0,
            rate_premium=55.0,
            min_charge=75.0,
        )
        assert cy_mid == 4.0
        assert price_low == 122.5  # 3.5 * 35
        assert price_high == 180.0  # 4.5 * 40
        assert special_items == []
        assert min_applied is False

    def test_min_charge_applied(self):
        price_low, price_high, cy_mid, special_items, min_applied = calculate_price(
            {
                "job_type": "standard",
                "totals": {"cubic_yards_mid": 1.0, "cubic_yards_low": 0.8, "cubic_yards_high": 1.2},
                "conditions": [],
                "items": [],
            },
            rate_low=35.0,
            rate_high=40.0,
            rate_premium=55.0,
            min_charge=75.0,
        )
        assert price_low == 75.0
        assert price_high == 75.0  # both clamped to min, no forced 1.5x spread
        assert min_applied is True

    def test_premium_job_uses_premium_rate(self):
        price_low, price_high, cy_mid, _, _ = calculate_price(
            {
                "job_type": "premium",
                "totals": {"cubic_yards_mid": 5.0, "cubic_yards_low": 4.5, "cubic_yards_high": 5.5},
                "conditions": [],
                "items": [],
            },
            rate_low=35.0,
            rate_high=40.0,
            rate_premium=55.0,
            min_charge=75.0,
        )
        assert price_low == 247.5
        assert price_high == 302.5

    def test_stairs_triggers_premium(self):
        price_low, _, _, _, _ = calculate_price(
            {
                "job_type": "standard",
                "totals": {"cubic_yards_mid": 3.0, "cubic_yards_low": 2.5, "cubic_yards_high": 3.5},
                "conditions": ["stairs"],
                "items": [],
            },
            rate_low=35.0,
            rate_high=40.0,
            rate_premium=55.0,
            min_charge=75.0,
        )
        assert price_low == 137.5

    def test_large_cy_triggers_premium(self):
        price_low, _, _, _, _ = calculate_price(
            {
                "job_type": "standard",
                "totals": {"cubic_yards_mid": 12.0, "cubic_yards_low": 11.0, "cubic_yards_high": 13.0},
                "conditions": [],
                "items": [],
            },
            rate_low=35.0,
            rate_high=40.0,
            rate_premium=55.0,
            min_charge=75.0,
        )
        assert price_low == 605.0

    def test_special_items_extracted(self):
        _, _, _, special_items, _ = calculate_price(
            {
                "job_type": "standard",
                "totals": {"cubic_yards_mid": 3.0, "cubic_yards_low": 2.5, "cubic_yards_high": 3.5},
                "conditions": [],
                "items": [
                    {"name": "Couch", "quantity": 1, "is_special": False, "cubic_yards": 2.0},
                    {"name": "Freon Tank", "quantity": 2, "is_special": True, "cubic_yards": 0.1},
                    {"name": "Paint Cans", "quantity": 5, "is_special": True, "cubic_yards": 0.3},
                ],
            },
        )
        assert len(special_items) == 2
        assert special_items[0]["name"] == "Freon Tank"
        assert special_items[0]["quantity"] == 2
        assert special_items[1]["quantity"] == 5

    def test_narrow_range_uses_actual_values(self):
        price_low, price_high, _, _, _ = calculate_price(
            {
                "job_type": "standard",
                "totals": {"cubic_yards_mid": 2.0, "cubic_yards_low": 1.99, "cubic_yards_high": 2.01},
                "conditions": [],
                "items": [],
            },
            rate_low=35.0,
            rate_high=40.0,
            min_charge=75.0,
        )
        assert price_low == 75.0
        assert price_high == 80.4

    def test_market_rates_blended(self):
        price_low, price_high, _, _, _ = calculate_price(
            {
                "job_type": "standard",
                "totals": {"cubic_yards_mid": 4.0, "cubic_yards_low": 3.5, "cubic_yards_high": 4.5},
                "conditions": [],
                "items": [],
            },
            rate_low=35.0,
            rate_high=40.0,
            market_rates={"source": "live_market_search", "low": 45.0, "high": 55.0, "premium": 70.0},
        )
        assert price_low == 157.5
        assert price_high == 247.5

    def test_market_rates_dont_underprice(self):
        price_low, _, _, _, _ = calculate_price(
            {
                "job_type": "standard",
                "totals": {"cubic_yards_mid": 4.0, "cubic_yards_low": 3.5, "cubic_yards_high": 4.5},
                "conditions": [],
                "items": [],
            },
            rate_low=50.0,
            rate_high=60.0,
            market_rates={"source": "live_market_search", "low": 30.0, "high": 40.0, "premium": 45.0},
        )
        assert price_low == 175.0

    def test_defaults_with_minimal_data(self):
        price_low, _, cy_mid, _, _ = calculate_price({})
        assert price_low == 75.0
        assert cy_mid > 0

    def test_heavy_items_condition_triggers_premium(self):
        price_low, _, _, _, _ = calculate_price(
            {
                "job_type": "standard",
                "totals": {"cubic_yards_mid": 3.0, "cubic_yards_low": 2.5, "cubic_yards_high": 3.5},
                "conditions": ["heavy_items"],
                "items": [],
            },
            rate_low=35.0,
            rate_premium=55.0,
        )
        assert price_low == 137.5  # 2.5 * 55

    def test_hoarder_condition_triggers_premium(self):
        price_low, _, _, _, _ = calculate_price(
            {
                "job_type": "standard",
                "totals": {"cubic_yards_mid": 3.0, "cubic_yards_low": 2.5, "cubic_yards_high": 3.5},
                "conditions": ["hoarder"],
                "items": [],
            },
            rate_low=35.0,
            rate_premium=55.0,
        )
        assert price_low == 137.5
