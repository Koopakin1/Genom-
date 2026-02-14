"""
Roles — Определение ролей ЖКХ (Костюмы/Modelfile-промпты).

Каждая роль определяет:
- Ollama model name (зарегистрированный через Modelfile)
- Описание функций
- Допустимые типы задач
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class WorkerRole(str, Enum):
    SYSADMIN = "sysadmin"
    AUDITOR = "auditor"
    ECONOMIST = "economist"
    CLEANER = "cleaner"
    MCHS = "mchs"


@dataclass
class RoleConfig:
    """Конфигурация роли ЖКХ."""
    role: WorkerRole
    ollama_model: str          # Имя модели в Ollama (genome-worker-<role>)
    description: str
    allowed_tasks: list[str]   # Типы задач, которые может выполнять
    temperature: float = 0.2
    max_tokens: int = 4096
    priority_boost: bool = False  # Роли с boost обрабатываются вне очереди

    def to_dict(self) -> dict:
        return {
            "role": self.role.value,
            "ollama_model": self.ollama_model,
            "description": self.description,
            "allowed_tasks": self.allowed_tasks,
            "priority_boost": self.priority_boost,
        }


# ==========================================
# Реестр ролей
# ==========================================

ROLE_REGISTRY: dict[WorkerRole, RoleConfig] = {
    WorkerRole.SYSADMIN: RoleConfig(
        role=WorkerRole.SYSADMIN,
        ollama_model="genome-worker-sysadmin",
        description="Системный администратор: Docker, сеть, файловая система, конфиги",
        allowed_tasks=["infra_setup", "docker_operation", "config_gen", "api_setup", "diagnostics"],
        temperature=0.2,
    ),
    WorkerRole.AUDITOR: RoleConfig(
        role=WorkerRole.AUDITOR,
        ollama_model="genome-worker-auditor",
        description="Ревизор: статический анализ кода, проверка безопасности, аудит Quality",
        allowed_tasks=["code_analysis", "security_check", "quality_audit", "validation"],
        temperature=0.1,
    ),
    WorkerRole.ECONOMIST: RoleConfig(
        role=WorkerRole.ECONOMIST,
        ollama_model="genome-worker-economist",
        description="Экономист: оценка ресурсоёмкости, прогнозирование, оптимизация",
        allowed_tasks=["cost_estimate", "resource_forecast", "optimization"],
        temperature=0.2,
    ),
    WorkerRole.CLEANER: RoleConfig(
        role=WorkerRole.CLEANER,
        ollama_model="genome-worker-cleaner",
        description="Ассенизатор: очистка мусора, кэшей, логов, Docker GC",
        allowed_tasks=["cleanup", "gc", "log_rotation", "disk_analysis"],
        temperature=0.1,
    ),
    WorkerRole.MCHS: RoleConfig(
        role=WorkerRole.MCHS,
        ollama_model="genome-worker-mchs",
        description="МЧС: аварийное реагирование, заморозка процессов, охлаждение",
        allowed_tasks=["emergency", "kill_process", "cooldown", "incident_report"],
        temperature=0.0,
        priority_boost=True,
    ),
}


def get_role_for_task(task_type: str) -> WorkerRole | None:
    """Подобрать наиболее подходящую роль для типа задачи."""
    for role, config in ROLE_REGISTRY.items():
        if task_type in config.allowed_tasks:
            return role
    return None


def get_role_config(role: WorkerRole) -> RoleConfig:
    """Получить конфигурацию роли."""
    return ROLE_REGISTRY[role]
