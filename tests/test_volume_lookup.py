from services.volume_lookup import validate_estimate


class TestValidateEstimate:
    def test_passthrough_empty_items(self):
        data = {"items": [], "totals": {"cubic_yards_mid": 3.0}}
        result = validate_estimate(data)
        assert result["items"] == []

    def test_passthrough_non_dict(self):
        assert validate_estimate("not a dict") == "not a dict"
        assert validate_estimate(None) is None

    def test_lookup_applied_to_five_gallon_bucket(self):
        data = {
            "items": [
                {"name": "5-Gallon Bucket", "cubic_yards": 0.5, "quantity": 3},
            ],
            "totals": {"cubic_yards_mid": 1.5, "cubic_yards_low": 1.0, "cubic_yards_high": 2.0},
        }
        result = validate_estimate(data)
        assert result["items"][0]["cubic_yards"] == 0.025
        assert result["items"][0].get("volume_lookup_applied") is True

    def test_lookup_applied_to_pallet(self):
        data = {
            "items": [
                {"name": "Wooden Pallet", "cubic_yards": 1.0, "quantity": 2},
            ],
            "totals": {"cubic_yards_mid": 2.0, "cubic_yards_low": 1.5, "cubic_yards_high": 2.5},
        }
        result = validate_estimate(data)
        assert result["items"][0]["cubic_yards"] == 0.15
        assert result["items"][0].get("volume_lookup_applied") is True

    def test_phantom_misc_removed(self):
        data = {
            "items": [
                {"name": "Couch", "cubic_yards": 3.0, "quantity": 1},
                {"name": "Miscellaneous Debris", "cubic_yards": 50.0, "quantity": 1},
            ],
            "totals": {"cubic_yards_mid": 53.0, "cubic_yards_low": 50.0, "cubic_yards_high": 56.0},
        }
        result = validate_estimate(data)
        names = [it["name"] for it in result["items"]]
        assert "Miscellaneous Debris" not in names
        assert "Couch" in names

    def test_non_phantom_misc_kept(self):
        data = {
            "items": [
                {"name": "Couch", "cubic_yards": 3.0, "quantity": 1},
                {"name": "Miscellaneous Debris", "cubic_yards": 1.0, "quantity": 1},
            ],
            "totals": {"cubic_yards_mid": 4.0, "cubic_yards_low": 3.5, "cubic_yards_high": 4.5},
        }
        result = validate_estimate(data)
        names = [it["name"] for it in result["items"]]
        assert "Miscellaneous Debris" in names

    def test_totals_synced_to_items(self):
        data = {
            "items": [
                {"name": "Couch", "cubic_yards": 3.0, "quantity": 1},
                {"name": "Chair", "cubic_yards": 1.0, "quantity": 2},
            ],
            "totals": {"cubic_yards_mid": 100.0, "cubic_yards_low": 80.0, "cubic_yards_high": 120.0},
        }
        result = validate_estimate(data)
        assert result["totals"]["cubic_yards_mid"] == 5.0  # 3*1 + 1*2
        assert result["totals"]["cubic_yards_low"] < 5.0
        assert result["totals"]["cubic_yards_high"] > 5.0

    def test_deep_copy_no_mutation(self):
        original = {
            "items": [{"name": "Couch", "cubic_yards": 3.0, "quantity": 1}],
            "totals": {"cubic_yards_mid": 3.0},
        }
        original_totals = original["totals"]["cubic_yards_mid"]
        validate_estimate(original)
        assert original["totals"]["cubic_yards_mid"] == original_totals

    def test_railroad_tie_lookup(self):
        data = {
            "items": [
                {"name": "Railroad Ties", "cubic_yards": 2.0, "quantity": 5},
            ],
            "totals": {"cubic_yards_mid": 10.0, "cubic_yards_low": 8.0, "cubic_yards_high": 12.0},
        }
        result = validate_estimate(data)
        assert result["items"][0]["cubic_yards"] == 0.17

    def test_quantity_multiplied_correctly(self):
        data = {
            "items": [
                {"name": "5-Gallon Bucket", "cubic_yards": 0.5, "quantity": 10},
                {"name": "Box Spring", "cubic_yards": 1.5, "quantity": 1},
            ],
            "totals": {"cubic_yards_mid": 6.5, "cubic_yards_low": 5.0, "cubic_yards_high": 8.0},
        }
        result = validate_estimate(data)
        expected_sum = 0.025 * 10 + 1.5 * 1
        assert abs(result["totals"]["cubic_yards_mid"] - expected_sum) < 0.1
