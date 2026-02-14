"""
Unit Economy — Система ресурсных «Юнитов».

Формула: Cost = (RAM_GB * Time_Sec) + (CPU_Load% * K)
Бюджетирование и контроль допустимости задач.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.resource_monitor import SystemSnapshot

logger = logging.getLogger("genome.unit_economy")

# Коэффициенты стоимости
CPU_COEFF = 0.5
RAM_COEFF = 1.0
TIME_COEFF = 0.1
BUDGET_SAFETY_MARGIN = 0.85  # Не расходовать больше 85% доступных ресурсов


@dataclass
class TaskCost:
    """Оценка стоимости задачи."""
    ram_gb: float
    cpu_percent: float
    time_sec: float
    total_units: float
    feasible: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ram_gb": self.ram_gb,
            "cpu_pct": self.cpu_percent,
            "time_sec": self.time_sec,
            "total_units": round(self.total_units, 2),
            "feasible": self.feasible,
            "reason": self.reason,
        }


# Приблизительные профили ресурсов для типов задач
TASK_PROFILES: dict[str, dict] = {
    "llm_inference_1.5b": {"ram_gb": 1.5, "cpu_pct": 60, "time_sec": 15},
    "llm_inference_8b": {"ram_gb": 5.0, "cpu_pct": 90, "time_sec": 60},
    "code_analysis": {"ram_gb": 0.5, "cpu_pct": 30, "time_sec": 10},
    "docker_operation": {"ram_gb": 0.3, "cpu_pct": 20, "time_sec": 5},
    "cleanup": {"ram_gb": 0.1, "cpu_pct": 10, "time_sec": 3},
    "default": {"ram_gb": 1.0, "cpu_pct": 40, "time_sec": 30},
}


def calculate_units(ram_gb: float, cpu_pct: float, time_sec: float) -> float:
    """Рассчитать стоимость в Юнитах."""
    return (ram_gb * RAM_COEFF * time_sec * TIME_COEFF) + (cpu_pct * CPU_COEFF)


def estimate_task_cost(
    task_type: str,
    snapshot: SystemSnapshot,
    custom_profile: dict | None = None,
) -> TaskCost:
    """
    Оценить стоимость задачи и проверить допустимость.

    Args:
        task_type: Тип задачи (ключ из TASK_PROFILES или кастомный)
        snapshot: Текущий снимок системы
        custom_profile: Кастомный профиль {ram_gb, cpu_pct, time_sec}
    """
    profile = custom_profile or TASK_PROFILES.get(task_type, TASK_PROFILES["default"])
    ram_gb = profile["ram_gb"]
    cpu_pct = profile["cpu_pct"]
    time_sec = profile["time_sec"]

    total_units = calculate_units(ram_gb, cpu_pct, time_sec)

    # Проверка допустимости
    available_ram_gb = snapshot.ram_available_mb / 1024
    safe_ram = available_ram_gb * BUDGET_SAFETY_MARGIN
    safe_cpu = (100 - snapshot.cpu_percent) * BUDGET_SAFETY_MARGIN

    feasible = True
    reason = ""

    if ram_gb > safe_ram:
        feasible = False
        reason = f"INSUFFICIENT_FUNDS: нужно {ram_gb:.1f} ГБ RAM, доступно {safe_ram:.1f} ГБ"
    elif cpu_pct > safe_cpu:
        feasible = False
        reason = f"INSUFFICIENT_FUNDS: нужно {cpu_pct}% CPU, доступно {safe_cpu:.0f}%"
    elif snapshot.is_critical:
        feasible = False
        reason = "SYSTEM_CRITICAL: система в критическом состоянии"

    if not feasible:
        logger.warning(f"Задача {task_type} отклонена: {reason}")
    else:
        logger.info(f"Задача {task_type}: {total_units:.1f} юнитов, допустима")

    return TaskCost(
        ram_gb=ram_gb,
        cpu_percent=cpu_pct,
        time_sec=time_sec,
        total_units=total_units,
        feasible=feasible,
        reason=reason,
    )
