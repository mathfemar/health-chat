"""Gera gráficos PNG pra enviar pelo Telegram. Usa matplotlib em modo Agg (sem GUI)."""
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import date, datetime


def daily_intake_vs_goal(per_day: list[dict], goal_kcal: int | None,
                          title: str = "Últimos dias") -> bytes:
    """per_day: [{date, intake_kcal, burned_kcal, net_kcal}].
    Retorna PNG bytes.
    """
    if not per_day:
        return _empty_chart("Sem dados no período.")

    dates = [_parse_date(d["date"]) for d in per_day]
    intake = [float(d["intake_kcal"]) for d in per_day]
    burned = [float(d["burned_kcal"]) for d in per_day]

    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=120)
    fig.patch.set_facecolor("white")

    width = 0.7
    # barras de intake (verde claro)
    ax.bar(dates, intake, width=width, color="#2A9D8F", alpha=0.85,
           label="Intake (kcal)", edgecolor="white")
    # queimadas como barras negativas pra ficar visualmente claro
    burned_neg = [-b for b in burned]
    ax.bar(dates, burned_neg, width=width, color="#E76F51", alpha=0.85,
           label="Queimado (kcal)", edgecolor="white")

    # linha da meta
    if goal_kcal:
        ax.axhline(goal_kcal, color="#2E86AB", linestyle="--", linewidth=1.5,
                   label=f"Meta: {goal_kcal} kcal", alpha=0.8)
        ax.axhline(0, color="gray", linewidth=0.5)

    ax.set_title(title, fontsize=14, fontweight="bold", color="#264653")
    ax.set_ylabel("kcal")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="upper left", framealpha=0.9, fontsize=9)

    # formato datas
    if len(dates) > 1:
        if len(dates) <= 14:
            ax.xaxis.set_major_locator(mdates.DayLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        else:
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        plt.xticks(rotation=45, ha="right")

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def daily_progress(
    intake_kcal: float,
    intake_protein_g: float,
    intake_carbs_g: float,
    intake_fat_g: float,
    burned_kcal: float,
    goal_kcal: int | None,
    goal_protein_g: int | None,
    eatback_pct: int = 100,
    meals: list[dict] | None = None,
    title: str = "Hoje",
) -> bytes:
    """Gráfico bonito do dia: anel central de kcal + barras de macros + breakdown
    por refeição. meals: [{'name': 'almoço', 'kcal': 700}].
    """
    fig = plt.figure(figsize=(10, 6), dpi=130, facecolor="white")
    gs = fig.add_gridspec(2, 2, width_ratios=[1.2, 1], height_ratios=[2, 1],
                          hspace=0.45, wspace=0.35)

    # ===== Anel central (kcal) =====
    ax_ring = fig.add_subplot(gs[0, 0])
    ax_ring.set_aspect("equal")
    ax_ring.set_xlim(-1.3, 1.3); ax_ring.set_ylim(-1.3, 1.3)
    ax_ring.set_axis_off()

    # Aplica eat-back se houver
    bonus = round(burned_kcal * (eatback_pct / 100)) if burned_kcal else 0
    adjusted_goal = (goal_kcal or 0) + bonus

    if adjusted_goal > 0:
        frac = min(intake_kcal / adjusted_goal, 1.5)  # cap visual em 150%
        over = intake_kcal > adjusted_goal
        # background ring (cinza)
        from matplotlib.patches import Wedge
        ax_ring.add_patch(Wedge((0, 0), 1.0, 0, 360, width=0.28,
                                facecolor="#E8E8E8", edgecolor="none"))
        # arco de progresso
        color = "#E76F51" if over else ("#2A9D8F" if frac >= 0.5 else "#2E86AB")
        sweep = min(frac, 1.0) * 360
        ax_ring.add_patch(Wedge((0, 0), 1.0, 90 - sweep, 90, width=0.28,
                                facecolor=color, edgecolor="none"))
        # se excedeu, arco extra vermelho fino
        if over and frac > 1.0:
            extra = min(frac - 1.0, 0.5) * 360
            ax_ring.add_patch(Wedge((0, 0), 1.05, 90 - extra, 90, width=0.04,
                                    facecolor="#9B2226", edgecolor="none"))

        # Texto central
        ax_ring.text(0, 0.18, f"{intake_kcal:.0f}", ha="center", va="center",
                     fontsize=32, fontweight="bold", color="#264653")
        ax_ring.text(0, -0.05, f"/ {adjusted_goal} kcal", ha="center", va="center",
                     fontsize=11, color="#666")
        remaining = adjusted_goal - intake_kcal
        if remaining >= 0:
            ax_ring.text(0, -0.30, f"{remaining:.0f} restantes",
                         ha="center", va="center", fontsize=12,
                         color="#2A9D8F", fontweight="bold")
        else:
            ax_ring.text(0, -0.30, f"{-remaining:.0f} acima",
                         ha="center", va="center", fontsize=12,
                         color="#E76F51", fontweight="bold")
        if bonus > 0:
            ax_ring.text(0, -0.55, f"(+{bonus} treino)", ha="center", va="center",
                         fontsize=9, color="#999", style="italic")
    else:
        ax_ring.text(0, 0, "sem meta\nainda", ha="center", va="center",
                     fontsize=14, color="#999")

    # ===== Barras de macros =====
    ax_macros = fig.add_subplot(gs[0, 1])
    macro_names = ["Proteína", "Carbo", "Gordura"]
    intakes = [intake_protein_g, intake_carbs_g, intake_fat_g]
    # Targets: proteína do perfil; carb/gordura estimados pela divisão sobrante
    if goal_kcal and goal_protein_g:
        remaining_kcal = max(0, goal_kcal - goal_protein_g * 4)
        target_c = round(remaining_kcal * 0.5 / 4)
        target_f = round(remaining_kcal * 0.5 / 9)
        targets = [goal_protein_g, target_c, target_f]
    else:
        targets = [None, None, None]

    colors = ["#A23B72", "#E9C46A", "#F4A261"]
    y_pos = list(range(len(macro_names)))
    bar_h = 0.55
    for i, (name, intake, target, c) in enumerate(zip(macro_names, intakes, targets, colors)):
        if target:
            ax_macros.barh(i, target, height=bar_h, color="#EEE", edgecolor="none", zorder=1)
            ax_macros.barh(i, min(intake, target * 1.2), height=bar_h,
                           color=c, edgecolor="none", zorder=2, alpha=0.95)
            ax_macros.text(target * 1.02, i, f"{intake:.0f}/{target}g",
                           va="center", ha="left", fontsize=10, color="#333")
        else:
            ax_macros.barh(i, intake, height=bar_h, color=c, edgecolor="none")
            ax_macros.text(intake + 1, i, f"{intake:.0f}g",
                           va="center", ha="left", fontsize=10)

    ax_macros.set_yticks(y_pos)
    ax_macros.set_yticklabels(macro_names, fontsize=11)
    ax_macros.invert_yaxis()
    ax_macros.spines[["top", "right", "bottom"]].set_visible(False)
    ax_macros.tick_params(left=False, bottom=False, labelbottom=False)
    if any(targets):
        max_t = max([t for t in targets if t] + [1])
        ax_macros.set_xlim(0, max_t * 1.45)

    # ===== Lista de refeições (parte de baixo) =====
    ax_meals = fig.add_subplot(gs[1, :])
    ax_meals.set_axis_off()
    if meals:
        labels = [f"{m.get('time', '??')}  {(m.get('label') or 'refeição')[:30]}" for m in meals[:6]]
        values = [m["kcal"] for m in meals[:6]]
        # Bar empilhado horizontal pra mostrar proporção entre refeições
        left = 0
        total = sum(values) or 1
        palette = ["#2E86AB", "#A23B72", "#2A9D8F", "#E9C46A", "#F4A261", "#E76F51"]
        for i, (lab, val) in enumerate(zip(labels, values)):
            ax_meals.barh(0, val, left=left, height=0.5,
                          color=palette[i % len(palette)], edgecolor="white", linewidth=2)
            # label inline se couber
            if val / total > 0.08:
                ax_meals.text(left + val / 2, 0,
                              f"{lab.split('  ',1)[-1][:14]}\n{val:.0f}",
                              ha="center", va="center", fontsize=8,
                              color="white", fontweight="bold")
            left += val
        ax_meals.set_xlim(0, total * 1.02)
        ax_meals.set_ylim(-0.7, 0.7)
        ax_meals.set_title("Refeições do dia", fontsize=10, color="#666", loc="left", pad=2)
    else:
        ax_meals.text(0.5, 0.5, "Sem refeições logadas hoje", ha="center", va="center",
                      fontsize=11, color="#999", transform=ax_meals.transAxes)

    fig.suptitle(title, fontsize=15, fontweight="bold", color="#264653", y=0.98)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def weight_trend(logs: list[dict], target_kg: float | None,
                  title: str = "Peso") -> bytes:
    """logs: [{'measured_at': datetime, 'weight_kg': float}], ordenado por data crescente.
    Plota pontos diários + média móvel de 7 dias + linha da meta.
    """
    if not logs:
        return _empty_chart("Sem pesagens registradas.")

    dates = [l["measured_at"] if isinstance(l["measured_at"], (datetime,)) else _parse_date(l["measured_at"]) for l in logs]
    weights = [float(l["weight_kg"]) for l in logs]

    # média móvel de 7 dias (janela de 7 pontos, não 7 dias calendário —
    # simplificação razoável; se pesar diariamente fica certinho)
    ma = []
    window = 7
    for i in range(len(weights)):
        start = max(0, i - window + 1)
        chunk = weights[start:i + 1]
        ma.append(sum(chunk) / len(chunk))

    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=120)
    fig.patch.set_facecolor("white")

    # scatter dos pontos diários
    ax.scatter(dates, weights, color="#A23B72", s=35, alpha=0.55,
               label="Pesagens", zorder=3)
    # linha da média móvel
    ax.plot(dates, ma, color="#2E86AB", linewidth=2.5, label="Média 7d", zorder=4)

    # meta
    if target_kg:
        ax.axhline(target_kg, color="#2A9D8F", linestyle="--", linewidth=1.5,
                   alpha=0.8, label=f"Meta: {target_kg} kg")

    # delta no canto
    if len(weights) >= 2:
        delta = weights[-1] - weights[0]
        ax.text(0.02, 0.97, f"Δ período: {delta:+.1f} kg",
                transform=ax.transAxes, fontsize=10, verticalalignment="top",
                bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                          edgecolor="gray", alpha=0.9))

    ax.set_title(title, fontsize=14, fontweight="bold", color="#264653")
    ax.set_ylabel("kg")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="upper right", framealpha=0.9, fontsize=9)

    if len(dates) > 1:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        plt.xticks(rotation=45, ha="right")

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _parse_date(d) -> date:
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        return datetime.fromisoformat(d[:10]).date()
    raise ValueError(f"Não consegui parsear data: {d!r}")


def _empty_chart(msg: str) -> bytes:
    fig, ax = plt.subplots(figsize=(8, 3), dpi=120)
    ax.text(0.5, 0.5, msg, ha="center", va="center", fontsize=14, color="#666")
    ax.set_axis_off()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
