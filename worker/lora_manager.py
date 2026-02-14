"""
LoRA Manager — Управление «костюмами» (Modelfile-ролями).

В CPU/ROCm-режиме настоящие LoRA-адаптеры заменены на
Modelfile-конфигурации с разными системными промптами.
Этот модуль управляет:
- Регистрацией и обновлением Modelfile в Ollama
- Переключением ролей «на лету»
- Интеграцией с Genome Bank для версионирования
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from worker.roles import WorkerRole, ROLE_REGISTRY

logger = logging.getLogger("genome.lora_manager")

OLLAMA_URL = "http://localhost:11434"
MODELFILES_DIR = Path(__file__).parent.parent / "modelfiles"


class LoRAManager:
    """Менеджер «костюмов» (Modelfile-ролей)."""

    def __init__(self, ollama_url: str = OLLAMA_URL):
        self._ollama_url = ollama_url.rstrip("/")
        self._registered: set[str] = set()

    async def register_all_roles(self) -> dict[str, bool]:
        """Зарегистрировать все стандартные роли в Ollama."""
        results = {}

        # Администрация
        admin_result = await self._create_from_modelfile(
            "genome-admin", MODELFILES_DIR / "Modelfile.admin"
        )
        results["genome-admin"] = admin_result

        # Роли ЖКХ
        for role, config in ROLE_REGISTRY.items():
            modelfile_path = MODELFILES_DIR / f"Modelfile.{role.value}"
            if modelfile_path.exists():
                success = await self._create_from_modelfile(
                    config.ollama_model, modelfile_path
                )
                results[config.ollama_model] = success
            else:
                logger.warning(f"Modelfile не найден: {modelfile_path}")
                results[config.ollama_model] = False

        registered = sum(1 for v in results.values() if v)
        logger.info(f"📦 Зарегистрировано {registered}/{len(results)} моделей")
        return results

    async def register_custom_role(
        self,
        model_name: str,
        base_model: str,
        system_prompt: str,
        temperature: float = 0.2,
        num_ctx: int = 4096,
    ) -> bool:
        """Зарегистрировать кастомную роль (для новых геномов)."""
        modelfile_content = (
            f"FROM {base_model}\n\n"
            f"PARAMETER temperature {temperature}\n"
            f"PARAMETER num_ctx {num_ctx}\n\n"
            f'SYSTEM """{system_prompt}"""\n'
        )
        return await self._create_from_content(model_name, modelfile_content)

    async def update_role(
        self,
        role: WorkerRole,
        new_system_prompt: str | None = None,
        new_temperature: float | None = None,
    ) -> bool:
        """Обновить существующую роль (для пересменки)."""
        config = ROLE_REGISTRY.get(role)
        if not config:
            return False

        modelfile_path = MODELFILES_DIR / f"Modelfile.{role.value}"
        if not modelfile_path.exists():
            logger.error(f"Modelfile не найден: {modelfile_path}")
            return False

        content = modelfile_path.read_text()

        # Обновляем системный промпт если нужно
        if new_system_prompt:
            import re
            content = re.sub(
                r'SYSTEM """.*?"""',
                f'SYSTEM """{new_system_prompt}"""',
                content,
                flags=re.DOTALL,
            )

        # Обновляем температуру если нужно
        if new_temperature is not None:
            import re
            content = re.sub(
                r"PARAMETER temperature [\d.]+",
                f"PARAMETER temperature {new_temperature}",
                content,
            )

        # Сохраняем обновлённый Modelfile
        modelfile_path.write_text(content)

        # Пересоздаём модель в Ollama
        return await self._create_from_content(config.ollama_model, content)

    async def list_registered(self) -> list[str]:
        """Список зарегистрированных genome-моделей."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self._ollama_url}/api/tags")
                if resp.status_code == 200:
                    models = resp.json().get("models", [])
                    return [
                        m["name"] for m in models
                        if m["name"].startswith("genome-")
                    ]
        except Exception as e:
            logger.error(f"Ошибка получения моделей: {e}")
        return []

    async def delete_role(self, model_name: str) -> bool:
        """Удалить модель из Ollama."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.delete(
                    f"{self._ollama_url}/api/delete",
                    json={"name": model_name},
                )
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"Ошибка удаления модели {model_name}: {e}")
            return False

    async def _create_from_modelfile(self, model_name: str, path: Path) -> bool:
        """Создать модель из файла Modelfile."""
        if not path.exists():
            logger.error(f"Файл не найден: {path}")
            return False
        content = path.read_text()
        return await self._create_from_content(model_name, content)

    async def _create_from_content(self, model_name: str, content: str) -> bool:
        """Создать модель из содержимого Modelfile."""
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(
                    f"{self._ollama_url}/api/create",
                    json={"name": model_name, "modelfile": content, "stream": False},
                )
                if resp.status_code == 200:
                    self._registered.add(model_name)
                    logger.info(f"✅ Модель {model_name} зарегистрирована")
                    return True
                else:
                    logger.error(f"Ошибка создания {model_name}: {resp.text}")
                    return False
        except Exception as e:
            logger.error(f"Ошибка создания {model_name}: {e}")
            return False
