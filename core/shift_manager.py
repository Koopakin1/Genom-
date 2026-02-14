"""
Пересменка — Протокол горячей смены ролей Worker'а.

Обеспечивает:
1. Валидацию готовности новой роли (модель загружена, ресурсы доступны)
2. Handoff текущего контекста (состояние, незавершённые задачи)
3. Подтверждение новой роли (тест-промпт)
4. Журнал смен для аудита

Вдохновлено реальными протоколами пересменки на производстве.
"""

from __future__ import annotations

import asyncio
import json
import time
import logging
from dataclasses import dataclass
from enum import Enum

import httpx

from worker.roles import WorkerRole, ROLE_REGISTRY, get_role_config

logger = logging.getLogger("genome.shift")

OLLAMA_URL = "http://localhost:11434"


class ShiftStatus(str, Enum):
    PENDING = "pending"
    VALIDATING = "validating"
    HANDOFF = "handoff"
    TESTING = "testing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ShiftReport:
    """Отчёт о пересменке."""
    from_role: str
    to_role: str
    status: ShiftStatus
    started_at: float
    completed_at: float | None = None
    validation_ok: bool = False
    test_ok: bool = False
    handoff_data: dict | None = None
    error: str | None = None

    @property
    def duration_sec(self) -> float:
        end = self.completed_at or time.time()
        return end - self.started_at

    def to_dict(self) -> dict:
        return {
            "from_role": self.from_role,
            "to_role": self.to_role,
            "status": self.status.value,
            "duration_sec": round(self.duration_sec, 2),
            "validation_ok": self.validation_ok,
            "test_ok": self.test_ok,
            "error": self.error,
        }


class ShiftManager:
    """Управление пересменками."""

    def __init__(self):
        self._current_role: str = "none"
        self._shift_history: list[ShiftReport] = []

    @property
    def current_role(self) -> str:
        return self._current_role

    @property
    def history(self) -> list[dict]:
        return [s.to_dict() for s in self._shift_history[-20:]]

    async def execute_shift(
        self,
        from_role: WorkerRole | str,
        to_role: WorkerRole,
        handoff_context: dict | None = None,
    ) -> ShiftReport:
        """Выполнить полный протокол пересменки.

        Этапы:
        1. VALIDATING — проверка модели и ресурсов
        2. HANDOFF — передача контекста
        3. TESTING — тестовый промпт новой роли
        4. COMPLETED / FAILED
        """
        from_name = from_role.value if isinstance(from_role, WorkerRole) else str(from_role)
        to_name = to_role.value

        report = ShiftReport(
            from_role=from_name,
            to_role=to_name,
            status=ShiftStatus.PENDING,
            started_at=time.time(),
        )

        logger.info(f"🔄 Пересменка: {from_name} → {to_name}")

        # Этап 1: Валидация
        report.status = ShiftStatus.VALIDATING
        try:
            ok = await self._validate_role(to_role)
            report.validation_ok = ok
            if not ok:
                report.status = ShiftStatus.FAILED
                report.error = f"Модель для роли {to_name} не готова"
                report.completed_at = time.time()
                logger.error(f"❌ Пересменка ОТКАЗ: {report.error}")
                self._shift_history.append(report)
                return report
            logger.info(f"  ✅ Валидация: модель {to_name} готова")
        except Exception as e:
            report.status = ShiftStatus.FAILED
            report.error = str(e)
            report.completed_at = time.time()
            self._shift_history.append(report)
            return report

        # Этап 2: Handoff
        report.status = ShiftStatus.HANDOFF
        report.handoff_data = {
            "previous_role": from_name,
            "timestamp": time.time(),
            "context": handoff_context or {},
        }
        logger.info(f"  📋 Handoff: контекст передан ({len(json.dumps(handoff_context or {}))} bytes)")

        # Этап 3: Тестовый промпт
        report.status = ShiftStatus.TESTING
        try:
            test_ok = await self._test_role(to_role)
            report.test_ok = test_ok
            if not test_ok:
                report.status = ShiftStatus.FAILED
                report.error = "Тестовый промпт не прошёл"
                report.completed_at = time.time()
                logger.error(f"❌ Тест роли {to_name} провален")
                self._shift_history.append(report)
                return report
            logger.info(f"  ✅ Тест: роль {to_name} отвечает корректно")
        except Exception as e:
            report.status = ShiftStatus.FAILED
            report.error = f"Test error: {e}"
            report.completed_at = time.time()
            self._shift_history.append(report)
            return report

        # Успех
        report.status = ShiftStatus.COMPLETED
        report.completed_at = time.time()
        self._current_role = to_name
        self._shift_history.append(report)

        logger.info(
            f"  🎉 Пересменка завершена: {from_name} → {to_name} "
            f"({report.duration_sec:.1f}с)"
        )
        return report

    async def _validate_role(self, role: WorkerRole) -> bool:
        """Проверить что модель для роли доступна в Ollama."""
        role_conf = get_role_config(role)
        if not role_conf:
            return False

        model_name = role_conf.model_name
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            if resp.status_code != 200:
                return False
            models = [m["name"] for m in resp.json().get("models", [])]
            return model_name in models or f"{model_name}:latest" in models

    async def _test_role(self, role: WorkerRole) -> bool:
        """Отправить тестовый промпт для проверки роли."""
        role_conf = get_role_config(role)
        if not role_conf:
            return False

        test_prompts = {
            WorkerRole.SYSADMIN: "Кратко: какие Docker-команды используешь для мониторинга? (1 предложение)",
            WorkerRole.AUDITOR: "Кратко: что проверяешь при security-аудите? (1 предложение)",
            WorkerRole.ECONOMIST: "Кратко: как оцениваешь стоимость задачи в Юнитах? (1 предложение)",
            WorkerRole.CLEANER: "Кратко: что очищаешь в первую очередь? (1 предложение)",
            WorkerRole.MCHS: "Кратко: твоё первое действие при аварии? (1 предложение)",
        }

        prompt = test_prompts.get(role, "Подтверди свою готовность одним предложением.")

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": role_conf.model_name,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"num_predict": 50},  # Короткий ответ
                },
            )
            if resp.status_code != 200:
                return False
            response_text = resp.json().get("response", "")
            # Если модель ответила чем-то осмысленным — тест пройден
            return len(response_text.strip()) > 10
