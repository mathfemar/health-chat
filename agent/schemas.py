"""Schemas (TypedDict / dataclasses). Mantemos leve, sem pydantic, porque
o agent recebe dicts JSON do LLM e converte conforme uso."""
from typing import TypedDict, NotRequired


class Food(TypedDict):
    """Linha da tabela foods."""
    id: int
    source: str
    name: str
    kcal: float          # por 100g
    protein_g: float
    carbs_g: float
    fat_g: float


class Portion(TypedDict):
    name: str            # "filé pequeno"
    grams: NotRequired[float | None]
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float


class MealItem(TypedDict):
    """Item resolvido de uma refeição."""
    name_llm: str        # como a Vision LLM chamou
    food_id: NotRequired[int | None]
    food_name: NotRequired[str | None]
    portion_g: float
    portion_label: NotRequired[str | None]  # "filé médio" se veio de food_portions
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    source: str          # 'TACO' | 'VITAT' | 'estimativa' | 'sem dados'


class MenuItem(TypedDict):
    name: str
    description: str
    price: NotRequired[float | None]
    category: str
    estimated_ingredients: list[str]


class VitatHit(TypedDict):
    id: int
    name: str
    default_measure: NotRequired[str | None]
