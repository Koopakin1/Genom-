"""
Genome Bank — Депозитарий версий «костюмов» (ролей ЖКХ).

Хранит историю Modelfile-конфигураций, результаты тестирования,
и метрики качества каждой версии роли.
"""

from __future__ import annotations

import json
import time
import os
import shutil
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from enum import Enum

logger = logging.getLogger("genome.genome_bank")

BANK_DIR = Path(__file__).parent.parent / "genome_bank"


class GenomeStatus(str, Enum):
    """Статус генома (версии роли)."""
    CANDIDATE = "candidate"    # Новая версия, ждёт тестирования
    TESTING = "testing"        # В процессе тестирования
    APPROVED = "approved"      # Прошла тесты, допущена к эксплуатации
    REJECTED = "rejected"      # Провалила тесты
    ACTIVE = "active"          # Текущая активная версия
    ARCHIVED = "archived"      # Устаревшая, в архиве


@dataclass
class GenomeVersion:
    """Версия «генома» (конфигурации роли)."""
    role: str
    version: str               # Семантическая версия, напр. "1.0.0"
    status: str = GenomeStatus.CANDIDATE.value
    modelfile_content: str = ""
    system_prompt: str = ""
    parameters: dict = field(default_factory=dict)
    test_results: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)   # accuracy, latency, safety_score
    created_at: float = field(default_factory=time.time)
    tested_at: float | None = None
    approved_at: float | None = None
    notes: str = ""

    @property
    def genome_id(self) -> str:
        return f"{self.role}@{self.version}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> GenomeVersion:
        return cls(**data)


class GenomeBank:
    """Депозитарий версий ролей."""

    def __init__(self, bank_dir: Path | str | None = None):
        self._bank_dir = Path(bank_dir) if bank_dir else BANK_DIR
        self._bank_dir.mkdir(parents=True, exist_ok=True)
        self._registry_file = self._bank_dir / "registry.json"
        self._registry: dict[str, list[dict]] = self._load_registry()

    def _load_registry(self) -> dict:
        if self._registry_file.exists():
            with open(self._registry_file) as f:
                return json.load(f)
        return {}

    def _save_registry(self) -> None:
        with open(self._registry_file, "w") as f:
            json.dump(self._registry, f, indent=2, ensure_ascii=False)

    def register(self, genome: GenomeVersion) -> str:
        """Зарегистрировать новую версию генома."""
        role = genome.role
        if role not in self._registry:
            self._registry[role] = []

        # Проверяем уникальность версии
        for existing in self._registry[role]:
            if existing["version"] == genome.version:
                logger.warning(f"Версия {genome.genome_id} уже существует, перезаписываю")
                self._registry[role].remove(existing)
                break

        self._registry[role].append(genome.to_dict())

        # Сохраняем Modelfile на диск
        role_dir = self._bank_dir / role
        role_dir.mkdir(exist_ok=True)
        modelfile_path = role_dir / f"Modelfile.{genome.version}"
        if genome.modelfile_content:
            modelfile_path.write_text(genome.modelfile_content)

        self._save_registry()
        logger.info(f"📦 Геном зарегистрирован: {genome.genome_id}")
        return genome.genome_id

    def get_active(self, role: str) -> GenomeVersion | None:
        """Получить активную версию роли."""
        if role not in self._registry:
            return None
        for entry in reversed(self._registry[role]):
            if entry["status"] == GenomeStatus.ACTIVE.value:
                return GenomeVersion.from_dict(entry)
        # Fallback: последняя approved
        for entry in reversed(self._registry[role]):
            if entry["status"] == GenomeStatus.APPROVED.value:
                return GenomeVersion.from_dict(entry)
        return None

    def get_version(self, role: str, version: str) -> GenomeVersion | None:
        """Получить конкретную версию."""
        if role not in self._registry:
            return None
        for entry in self._registry[role]:
            if entry["version"] == version:
                return GenomeVersion.from_dict(entry)
        return None

    def get_history(self, role: str) -> list[GenomeVersion]:
        """Получить историю версий роли."""
        if role not in self._registry:
            return []
        return [GenomeVersion.from_dict(e) for e in self._registry[role]]

    def update_status(self, role: str, version: str, status: GenomeStatus,
                      test_results: dict | None = None,
                      metrics: dict | None = None) -> bool:
        """Обновить статус версии генома."""
        if role not in self._registry:
            return False

        for entry in self._registry[role]:
            if entry["version"] == version:
                entry["status"] = status.value
                if test_results:
                    entry["test_results"] = test_results
                if metrics:
                    entry["metrics"] = metrics
                if status == GenomeStatus.APPROVED:
                    entry["approved_at"] = time.time()
                if status in (GenomeStatus.TESTING, GenomeStatus.APPROVED, GenomeStatus.REJECTED):
                    entry["tested_at"] = time.time()

                # Если статус ACTIVE — деактивируем предыдущую
                if status == GenomeStatus.ACTIVE:
                    for other in self._registry[role]:
                        if other["version"] != version and other["status"] == GenomeStatus.ACTIVE.value:
                            other["status"] = GenomeStatus.ARCHIVED.value

                self._save_registry()
                logger.info(f"Геном {role}@{version} → {status.value}")
                return True
        return False

    def promote(self, role: str, version: str) -> bool:
        """Повысить approved-версию до active."""
        genome = self.get_version(role, version)
        if not genome:
            return False
        if genome.status != GenomeStatus.APPROVED.value:
            logger.warning(f"Нельзя промоутить {genome.genome_id}: статус {genome.status}")
            return False
        return self.update_status(role, version, GenomeStatus.ACTIVE)

    def rollback(self, role: str) -> GenomeVersion | None:
        """Откатиться к предыдущей approved-версии."""
        if role not in self._registry:
            return None

        approved_versions = [
            GenomeVersion.from_dict(e) for e in self._registry[role]
            if e["status"] in (GenomeStatus.APPROVED.value, GenomeStatus.ARCHIVED.value)
        ]
        if not approved_versions:
            return None

        # Берём предпоследнюю
        target = approved_versions[-1]
        self.update_status(role, target.version, GenomeStatus.ACTIVE)
        return target
