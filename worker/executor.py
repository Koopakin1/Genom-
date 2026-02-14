"""
Executor — Исполнитель ЖКХ.

Вызывает модель 8B через Ollama API с нужной ролью (Modelfile).
Stateless: получает контекст из задачи, возвращает результат.
"""

from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass

import httpx

from worker.roles import WorkerRole, RoleConfig, ROLE_REGISTRY

logger = logging.getLogger("genome.executor")

OLLAMA_BASE_URL = "http://localhost:11434"


@dataclass
class ExecutionResult:
    """Результат выполнения задачи ЖКХ."""
    task_id: str
    role: str
    success: bool
    output: str
    raw_response: dict | None = None
    duration_sec: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "success": self.success,
            "output": self.output,
            "duration_sec": round(self.duration_sec, 2),
            "error": self.error,
        }


class WorkerExecutor:
    """Исполнитель ЖКХ — вызов Ollama API."""

    def __init__(self, ollama_url: str = OLLAMA_BASE_URL):
        self._ollama_url = ollama_url.rstrip("/")
        self._current_role: WorkerRole | None = None

    @property
    def current_role(self) -> WorkerRole | None:
        return self._current_role

    async def check_health(self) -> bool:
        """Проверить доступность Ollama."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._ollama_url}/")
                return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        """Получить список загруженных моделей."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self._ollama_url}/api/tags")
                if resp.status_code == 200:
                    data = resp.json()
                    return [m["name"] for m in data.get("models", [])]
        except Exception as e:
            logger.error(f"Ошибка получения списка моделей: {e}")
        return []

    async def switch_role(self, role: WorkerRole) -> bool:
        """
        Переключить роль ЖКХ (сменить «костюм»).
        В CPU-only режиме это фактически выбор другой Ollama-модели.
        """
        config = ROLE_REGISTRY.get(role)
        if not config:
            logger.error(f"Неизвестная роль: {role}")
            return False

        # Проверяем, что модель доступна
        models = await self.list_models()
        if config.ollama_model not in models:
            logger.warning(
                f"Модель {config.ollama_model} не найдена в Ollama. "
                f"Доступные: {models}"
            )
            # Пробуем создать модель из Modelfile позже
            # Пока возвращаем True — модель может быть создана при первом вызове
            pass

        self._current_role = role
        logger.info(f"ЖКХ: роль переключена → {role.value} ({config.ollama_model})")
        return True

    async def execute(
        self,
        task_id: str,
        prompt: str,
        role: WorkerRole | None = None,
        context: str | None = None,
    ) -> ExecutionResult:
        """
        Выполнить задачу.

        Args:
            task_id: ID задачи
            prompt: Текст задачи
            role: Роль (если None — используется текущая)
            context: Дополнительный контекст из памяти
        """
        effective_role = role or self._current_role
        if effective_role is None:
            return ExecutionResult(
                task_id=task_id,
                role="none",
                success=False,
                output="",
                error="Роль не назначена. Вызовите switch_role() перед выполнением.",
            )

        config = ROLE_REGISTRY[effective_role]

        # Если роль изменилась — переключаемся
        if effective_role != self._current_role:
            await self.switch_role(effective_role)

        # Формируем промпт с контекстом
        full_prompt = prompt
        if context:
            full_prompt = f"КОНТЕКСТ:\n{context}\n\nЗАДАЧА:\n{prompt}"

        start_time = time.time()

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(
                    f"{self._ollama_url}/api/generate",
                    json={
                        "model": config.ollama_model,
                        "prompt": full_prompt,
                        "stream": False,
                        "options": {
                            "temperature": config.temperature,
                            "num_predict": config.max_tokens,
                        },
                    },
                )

                duration = time.time() - start_time

                if resp.status_code != 200:
                    return ExecutionResult(
                        task_id=task_id,
                        role=effective_role.value,
                        success=False,
                        output="",
                        error=f"Ollama HTTP {resp.status_code}: {resp.text}",
                        duration_sec=duration,
                    )

                data = resp.json()
                output = data.get("response", "")

                logger.info(
                    f"Задача {task_id} выполнена ролью {effective_role.value} "
                    f"за {duration:.1f}с"
                )

                return ExecutionResult(
                    task_id=task_id,
                    role=effective_role.value,
                    success=True,
                    output=output,
                    raw_response=data,
                    duration_sec=duration,
                )

        except httpx.TimeoutException:
            duration = time.time() - start_time
            return ExecutionResult(
                task_id=task_id,
                role=effective_role.value,
                success=False,
                output="",
                error=f"Таймаут Ollama ({duration:.0f}с)",
                duration_sec=duration,
            )
        except Exception as e:
            duration = time.time() - start_time
            return ExecutionResult(
                task_id=task_id,
                role=effective_role.value,
                success=False,
                output="",
                error=str(e),
                duration_sec=duration,
            )
