def validate_per_amount(per_amount: float) -> None:
    if per_amount <= 0:
        raise ValueError(f"per_amount must be > 0, got {per_amount}")


def normalize_per_100(
    kcal: float, protein: float, fat: float, carbs: float, per_amount: float
) -> dict:
    validate_per_amount(per_amount)
    factor = 100 / per_amount
    return {
        "kcal_per_100": round(kcal * factor, 1),
        "protein_per_100": round(protein * factor, 1),
        "fat_per_100": round(fat * factor, 1),
        "carbs_per_100": round(carbs * factor, 1),
    }


def validate_nutrition(
    kcal_per_100: float,
    protein_per_100: float,
    fat_per_100: float,
    carbs_per_100: float,
) -> list[str]:
    warnings = []
    for name, val in [
        ("kcal_per_100", kcal_per_100),
        ("protein_per_100", protein_per_100),
        ("fat_per_100", fat_per_100),
        ("carbs_per_100", carbs_per_100),
    ]:
        if val < 0:
            raise ValueError(f"{name} must be >= 0, got {val}")

    if kcal_per_100 > 900:
        warnings.append("unusually_high_calories")

    if protein_per_100 + fat_per_100 + carbs_per_100 > 105:
        warnings.append("macros_exceed_weight")

    macro_kcal = protein_per_100 * 4 + fat_per_100 * 9 + carbs_per_100 * 4
    if macro_kcal > 0:
        ratio = kcal_per_100 / macro_kcal
        if ratio < 0.5 or ratio > 1.5:
            warnings.append("calories_inconsistent_with_macros")

    return warnings


def validate_portion_weight(weight_grams: float) -> list[str]:
    warnings = []
    if weight_grams > 2000:
        warnings.append("portion_exceeds_2kg")
    return warnings


def calculate_portion(
    weight_grams: float,
    kcal_per_100: float,
    protein_per_100: float,
    fat_per_100: float,
    carbs_per_100: float,
) -> dict:
    factor = weight_grams / 100
    return {
        "kcal": round(kcal_per_100 * factor, 1),
        "protein": round(protein_per_100 * factor, 1),
        "fat": round(fat_per_100 * factor, 1),
        "carbs": round(carbs_per_100 * factor, 1),
    }
