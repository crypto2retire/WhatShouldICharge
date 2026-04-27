from services.volume_lookup import validate_estimate, apply_pile_adjustment, detect_heavy_materials


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
        # Bounds are floor-only now — couch at 3.0 CY stays (not capped), chair at 1.0 stays.
        # Total = 3.0*1 + 1.0*2 = 5.0
        assert result["totals"]["cubic_yards_mid"] == 5.0
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
        # 5-gallon bucket lookup → 0.025 each, box spring stays at 1.5 CY (floor-only bounds).
        expected_sum = 0.025 * 10 + 1.5 * 1
        assert abs(result["totals"]["cubic_yards_mid"] - expected_sum) < 0.1


class TestApplyPileAdjustment:
    def test_no_pile_field(self):
        data = {"items": [{"name": "Couch", "cubic_yards": 2.0, "quantity": 1}],
                "totals": {"cubic_yards_mid": 2.0}, "total_cubic_yards": 2.0}
        result, notes = apply_pile_adjustment(data)
        assert result["total_cubic_yards"] == 2.0
        assert notes == []

    def test_is_pile_false(self):
        data = {"items": [{"name": "Couch", "cubic_yards": 2.0, "quantity": 1}],
                "totals": {"cubic_yards_mid": 2.0}, "total_cubic_yards": 2.0,
                "pile_estimate": {"is_pile": False, "estimated_cy": 0}}
        result, notes = apply_pile_adjustment(data)
        assert result["total_cubic_yards"] == 2.0
        assert notes == []

    def test_pile_within_threshold_no_change(self):
        """Pile only 10% bigger than items — no boost."""
        data = {
            "items": [{"name": "Bag", "cubic_yards": 2.0, "quantity": 1}],
            "totals": {"cubic_yards_mid": 2.0, "cubic_yards_low": 1.7, "cubic_yards_high": 2.3},
            "pile_estimate": {"is_pile": True, "width_in": 36, "depth_in": 36,
                              "height_in": 36, "packing_factor": 0.65, "estimated_cy": 2.2},
        }
        result, notes = apply_pile_adjustment(data)
        assert abs(result["totals"]["cubic_yards_mid"] - 2.0) < 0.01
        assert notes == []

    def test_pile_boosts_undercount(self):
        """Pile 2.5x bigger than items — volume adjusted upward."""
        data = {
            "items": [{"name": "Bag", "cubic_yards": 2.0, "quantity": 1}],
            "totals": {"cubic_yards_mid": 2.0, "cubic_yards_low": 1.7, "cubic_yards_high": 2.3},
            "pile_estimate": {"is_pile": True, "width_in": 96, "depth_in": 72,
                              "height_in": 48, "packing_factor": 0.65, "estimated_cy": 5.0},
        }
        result, notes = apply_pile_adjustment(data)
        assert result["totals"]["cubic_yards_mid"] > 2.0
        assert result.get("pile_adjustment_applied") is True
        assert len(notes) > 0

    def test_confidence_penalty_applied(self):
        """Big gap between pile and items reduces confidence."""
        data = {
            "items": [{"name": "Bag", "cubic_yards": 1.0, "quantity": 1}],
            "totals": {"cubic_yards_mid": 1.0},
            "confidence": 80,
            "pile_estimate": {"is_pile": True, "width_in": 96, "depth_in": 72,
                              "height_in": 48, "packing_factor": 0.65, "estimated_cy": 5.0},
        }
        result, notes = apply_pile_adjustment(data)
        assert result["confidence"] < 80

    def test_confidence_never_below_50(self):
        """Confidence penalty hard-capped at 50."""
        data = {
            "items": [{"name": "Bag", "cubic_yards": 0.5, "quantity": 1}],
            "totals": {"cubic_yards_mid": 0.5},
            "confidence": 75,
            "pile_estimate": {"is_pile": True, "estimated_cy": 10.0},
        }
        result, notes = apply_pile_adjustment(data)
        assert result["confidence"] >= 50

    def test_boost_capped_at_max_factor(self):
        """Boost never exceeds 2.5x original item sum."""
        data = {
            "items": [{"name": "Bag", "cubic_yards": 1.0, "quantity": 1}],
            "totals": {"cubic_yards_mid": 1.0},
            "pile_estimate": {"is_pile": True, "estimated_cy": 50.0},
        }
        result, notes = apply_pile_adjustment(data)
        assert result["totals"]["cubic_yards_mid"] <= 1.0 * 2.5

    def test_no_items_uses_pile_directly(self):
        """When items are empty, pile estimate becomes the total."""
        data = {
            "items": [],
            "totals": {"cubic_yards_mid": 0},
            "pile_estimate": {"is_pile": True, "estimated_cy": 4.0},
        }
        result, notes = apply_pile_adjustment(data)
        assert abs(result["totals"]["cubic_yards_mid"] - 4.0) < 0.01
        assert len(notes) == 1


class TestDetectHeavyMaterials:
    def test_shingles_triggers_premium(self):
        data = {
            "items": [{"name": "roof shingles and debris", "cubic_yards": 0.4}],
            "job_type": "standard",
            "conditions": [],
        }
        found = detect_heavy_materials(data)
        assert found == ["shingle"]
        assert data["job_type"] == "premium"
        assert "heavy_items" in data["conditions"]

    def test_no_heavy_items(self):
        data = {
            "items": [{"name": "Couch", "cubic_yards": 2.0}],
            "job_type": "standard",
            "conditions": [],
        }
        found = detect_heavy_materials(data)
        assert found == []
        assert data["job_type"] == "standard"

    def test_concrete_triggers_premium(self):
        data = {
            "items": [{"name": "concrete chunks", "cubic_yards": 3.0}],
            "job_type": "standard",
            "conditions": [],
        }
        found = detect_heavy_materials(data)
        assert data["job_type"] == "premium"
        assert "heavy_items" in data["conditions"]

    def test_does_not_downgrade_existing_premium(self):
        data = {
            "items": [{"name": "shingles", "cubic_yards": 5.0}],
            "job_type": "hoarder",
            "conditions": ["stairs"],
        }
        detect_heavy_materials(data)
        assert data["job_type"] == "hoarder"

    def test_preserves_existing_conditions(self):
        data = {
            "items": [{"name": "brick and shingle debris", "cubic_yards": 2.0}],
            "job_type": "standard",
            "conditions": ["stairs"],
        }
        detect_heavy_materials(data)
        assert "stairs" in data["conditions"]
        assert "heavy_items" in data["conditions"]
