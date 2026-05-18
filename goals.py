"""Cálculo de meta calórica (Mifflin-St Jeor, padrão clínico moderno).

Determinístico — não passa pelo LLM. Tools chamam estas funções.
"""
from datetime import date

# ATENÇÃO: estes fatores representam APENAS a atividade NEAT
# (Non-Exercise Activity Thermogenesis — andar no trabalho, subir escada, etc).
# Exercício planejado é REGISTRADO SEPARADAMENTE via log_exercise.
# Valores são conservadores (vs. MFP/Cal AI) porque a literatura moderna com
# água duplamente marcada mostra que multiplicadores antigos superestimam.
ACTIVITY_FACTORS: dict[str, float] = {
    "sedentary":   1.20,  # mesa o dia inteiro, pouquíssimo movimento
    "light":       1.30,  # alguns passos, caminhadas curtas
    "moderate":    1.40,  # trabalho com circulação (professor, garçom, vendedor)
    "active":      1.50,  # trabalho físico (construção, entregador) OU muita atividade no dia
    "very_active": 1.65,  # trabalho muito demandante fisicamente, atleta NEAT alto
}

# 7700 kcal ≈ 1 kg de gordura. Padrão da indústria pra calcular déficit/superávit.
KCAL_PER_KG = 7700

# Pisos de segurança recomendados pela WHO/American College of Sports Medicine
MIN_KCAL_FEMALE = 1200
MIN_KCAL_MALE = 1500


def age_from_birth_date(birth: date) -> int:
    today = date.today()
    years = today.year - birth.year
    if (today.month, today.day) < (birth.month, birth.day):
        years -= 1
    return years


def mifflin_st_jeor(sex: str, weight_kg: float, height_cm: int, age: int) -> float:
    """BMR (taxa metabólica basal) em kcal/dia."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    if sex == "M":
        return base + 5
    # F ou O (não-binárie): mais conservador (F)
    return base - 161


def daily_goal_kcal(profile: dict) -> int | None:
    """Retorna meta calórica diária ou None se faltam dados."""
    required = ("sex", "current_weight_kg", "height_cm", "birth_date",
                "activity_level", "weekly_rate_kg")
    if any(profile.get(k) is None for k in required):
        return None

    age = age_from_birth_date(profile["birth_date"])
    bmr = mifflin_st_jeor(
        sex=profile["sex"],
        weight_kg=float(profile["current_weight_kg"]),
        height_cm=int(profile["height_cm"]),
        age=age,
    )
    tdee = bmr * ACTIVITY_FACTORS.get(profile["activity_level"], 1.55)
    # weekly_rate_kg negativo = perder peso = déficit; positivo = ganhar
    daily_delta = float(profile["weekly_rate_kg"]) * KCAL_PER_KG / 7
    goal = tdee + daily_delta
    floor = MIN_KCAL_MALE if profile["sex"] == "M" else MIN_KCAL_FEMALE
    return max(floor, round(goal))


def suggested_protein_g(profile: dict) -> int | None:
    """Sugestão simples: 1.6 g/kg corporal (faixa de ganho/manutenção muscular)."""
    w = profile.get("current_weight_kg")
    if w is None:
        return None
    return round(float(w) * 1.6)


def suggested_weekly_rate(profile: dict) -> float | None:
    """Sugere ritmo semanal seguro baseado em BMI atual.
    BMI > 30 (obesidade): -0.75 a -1.0 kg/sem é seguro
    BMI 27-30 (sobrepeso): -0.5 a -0.75 kg/sem
    BMI 22-27 (normal/levemente alto): -0.25 a -0.5 kg/sem
    BMI < 22: -0.25 ou 0 (recomp)
    """
    w = profile.get("current_weight_kg")
    h = profile.get("height_cm")
    if w is None or h is None:
        return None
    bmi = float(w) / ((float(h) / 100) ** 2)
    if bmi >= 30:   return -0.75
    if bmi >= 27:   return -0.5
    if bmi >= 22:   return -0.4
    return -0.25


def macros_split(daily_kcal: int, protein_g: int) -> dict:
    """Distribui kcal restantes em 50C / 50G após proteína. Heurística simples."""
    protein_kcal = protein_g * 4
    remaining = max(0, daily_kcal - protein_kcal)
    carbs_kcal = remaining * 0.5
    fat_kcal = remaining * 0.5
    return {
        "protein_g": protein_g,
        "carbs_g": round(carbs_kcal / 4),
        "fat_g": round(fat_kcal / 9),
    }
