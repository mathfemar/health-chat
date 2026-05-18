"""Mensagens proativas com personalidade. Tom: direto, levemente persuasivo,
1-2 emojis máximo, sem drama. Randomizadas pra não ficar repetitivo."""
import random


LUNCH_NUDGES = [
    "🍽 Hora do almoço. Já comeu ou tá montando? Manda foto/texto que eu loga.",
    "🥗 Cadê o almoço? Manda quando puder.",
    "Almoço logado hoje? 👀 Se sim, ignora. Se não, manda aí.",
    "🍴 Lunchtime. Se já comeu, manda foto/texto pra eu somar no diário.",
    "Tô curioso pra saber o almoço de hoje 🍛. Manda aí quando der.",
]


DINNER_NUDGES = [
    "🌙 E o jantar, como foi? Manda pra fechar o dia certo.",
    "Jantar logado? 🍽 Se já comeu, me conta.",
    "Faltando o jantar pra fechar o dia. 🍴 Manda foto ou texto.",
    "🥘 Cadê o jantar? Sem ele a meta fica torta.",
    "Recap rápido: bateu meta hoje? Manda o jantar que eu fecho a conta. 🌙",
]


FRIDAY_INTROS = [
    "🎉 Sextou! Vamos ver como foi a semana:",
    "📊 Sextou — chegou seu resumo semanal:",
    "Sextão. Sua semana em números 👇",
    "🔥 Bateu o sino. Resumo da semana pra você:",
]


def pick_lunch() -> str:
    return random.choice(LUNCH_NUDGES)


def pick_dinner() -> str:
    return random.choice(DINNER_NUDGES)


def pick_friday() -> str:
    return random.choice(FRIDAY_INTROS)
