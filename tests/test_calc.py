import pytest
from mcp_health.calc import (
    calculate_portion,
    normalize_per_100,
    validate_nutrition,
    validate_per_amount,
    validate_portion_weight,
)


class TestNormalizePer100:
    def test_per_100(self):
        result = normalize_per_100(200, 10, 5, 30, 100)
        assert result == {
            "kcal_per_100": 200,
            "protein_per_100": 10,
            "fat_per_100": 5,
            "carbs_per_100": 30,
        }

    def test_per_30(self):
        result = normalize_per_100(60, 3, 1.5, 9, 30)
        assert result["kcal_per_100"] == 200.0
        assert result["protein_per_100"] == 10.0
        assert result["fat_per_100"] == 5.0
        assert result["carbs_per_100"] == 30.0

    def test_per_250(self):
        result = normalize_per_100(500, 25, 12.5, 75, 250)
        assert result["kcal_per_100"] == 200.0
        assert result["protein_per_100"] == 10.0

    def test_zero_per_amount(self):
        with pytest.raises(ValueError):
            normalize_per_100(100, 5, 3, 10, 0)

    def test_negative_per_amount(self):
        with pytest.raises(ValueError):
            normalize_per_100(100, 5, 3, 10, -50)


class TestValidateNutrition:
    def test_valid(self):
        assert validate_nutrition(200, 10, 5, 30) == []

    def test_high_calories(self):
        warnings = validate_nutrition(950, 10, 5, 30)
        assert "unusually_high_calories" in warnings

    def test_macros_exceed_weight(self):
        warnings = validate_nutrition(500, 40, 40, 30)
        assert "macros_exceed_weight" in warnings

    def test_calories_inconsistent(self):
        # P*4+F*9+C*4 = 10*4+5*9+30*4 = 40+45+120 = 205; kcal=50 → ratio 50/205 ≈ 0.24
        warnings = validate_nutrition(50, 10, 5, 30)
        assert "calories_inconsistent_with_macros" in warnings

    def test_negative_value(self):
        with pytest.raises(ValueError):
            validate_nutrition(-10, 5, 3, 10)

    def test_zero_macros_no_crash(self):
        warnings = validate_nutrition(0, 0, 0, 0)
        assert warnings == []


class TestCalculatePortion:
    def test_basic(self):
        result = calculate_portion(150, 200, 10, 5, 30)
        assert result["kcal"] == 300.0
        assert result["protein"] == 15.0
        assert result["fat"] == 7.5
        assert result["carbs"] == 45.0

    def test_small_portion(self):
        result = calculate_portion(25, 400, 20, 10, 50)
        assert result["kcal"] == 100.0


class TestValidatePortionWeight:
    def test_normal(self):
        assert validate_portion_weight(500) == []

    def test_over_2kg(self):
        assert "portion_exceeds_2kg" in validate_portion_weight(2500)


class TestValidatePerAmount:
    def test_valid(self):
        validate_per_amount(100)  # no exception

    def test_zero(self):
        with pytest.raises(ValueError):
            validate_per_amount(0)
