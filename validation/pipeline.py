"""
Pipeline — Алгоритм «Пересменка» (Sequential Testing Pipeline).

Протокол валидации новых версий «костюмов» (ролей ЖКХ).

Алгоритм:
1. Зарегистрировать новый «геном» (кандидат)
2. Запустить набор контрольных задач для роли
3. Оценить результаты по метрикам качества
4. Сравнить с текущей активной версией
5. Если кандидат лучше — промоутить; иначе — отклонить
6. При промоушне — «Пересменка» (плавная замена в бою)
"""

from __future__ import annotations

import asyncio
import time
import logging
from dataclasses import dataclass, field

from validation.genome_bank import GenomeBank, GenomeVersion, GenomeStatus
from validation.test_suite import (
    TestCase, TestResult, evaluate_response,
    get_tests_for_role, STANDARD_TESTS,
)
from worker.executor import WorkerExecutor
from worker.roles import WorkerRole

logger = logging.getLogger("genome.pipeline")


@dataclass
class ValidationReport:
    """Отчёт о валидации генома."""
    genome_id: str
    role: str
    version: str
    total_tests: int
    passed_tests: int
    avg_score: float
    avg_response_sec: float
    test_results: list[dict] = field(default_factory=list)
    verdict: str = "pending"      # passed | failed | inconclusive
    comparison: dict | None = None  # Сравнение с действующей версией
    timestamp: float = field(default_factory=time.time)

    @property
    def pass_rate(self) -> float:
        return self.passed_tests / max(self.total_tests, 1)

    def to_dict(self) -> dict:
        return {
            "genome_id": self.genome_id,
            "role": self.role,
            "version": self.version,
            "total_tests": self.total_tests,
            "passed_tests": self.passed_tests,
            "pass_rate": round(self.pass_rate, 3),
            "avg_score": round(self.avg_score, 3),
            "avg_response_sec": round(self.avg_response_sec, 2),
            "verdict": self.verdict,
            "comparison": self.comparison,
            "test_results": self.test_results,
        }


class ValidationPipeline:
    """Алгоритм «Пересменка» — валидация и промоушн ролей."""

    def __init__(
        self,
        bank: GenomeBank,
        executor: WorkerExecutor,
        min_pass_rate: float = 0.7,
        min_avg_score: float = 0.6,
    ):
        self.bank = bank
        self.executor = executor
        self.min_pass_rate = min_pass_rate
        self.min_avg_score = min_avg_score

    async def validate_genome(
        self,
        role: str,
        version: str,
        custom_tests: list[TestCase] | None = None,
    ) -> ValidationReport:
        """
        Полный цикл валидации генома.

        1. Получить кандидата из банка
        2. Запустить контрольные задачи
        3. Оценить результаты
        4. Сравнить с текущей версией
        5. Вынести вердикт
        """
        genome = self.bank.get_version(role, version)
        if not genome:
            return ValidationReport(
                genome_id=f"{role}@{version}",
                role=role,
                version=version,
                total_tests=0,
                passed_tests=0,
                avg_score=0,
                avg_response_sec=0,
                verdict="error: genome not found",
            )

        # Обновляем статус на TESTING
        self.bank.update_status(role, version, GenomeStatus.TESTING)

        # Получаем тесты
        tests = custom_tests or get_tests_for_role(role)
        if not tests:
            logger.warning(f"Нет тестов для роли {role}")
            return ValidationReport(
                genome_id=genome.genome_id,
                role=role,
                version=version,
                total_tests=0,
                passed_tests=0,
                avg_score=0,
                avg_response_sec=0,
                verdict="inconclusive: no tests",
            )

        logger.info(f"🧬 Пересменка: валидация {genome.genome_id} ({len(tests)} тестов)")

        # Запускаем тесты последовательно (не параллельно, экономим ресурсы)
        results: list[TestResult] = []
        worker_role = None
        try:
            worker_role = WorkerRole(role)
        except ValueError:
            worker_role = WorkerRole.SYSADMIN  # fallback

        for i, test in enumerate(tests):
            logger.info(f"  📋 Тест {i+1}/{len(tests)}: {test.test_id}")

            exec_result = await self.executor.execute(
                task_id=f"val_{test.test_id}",
                prompt=test.prompt,
                role=worker_role,
            )

            if exec_result.success:
                test_result = evaluate_response(
                    test, exec_result.output, exec_result.duration_sec
                )
            else:
                test_result = TestResult(
                    test_id=test.test_id,
                    passed=False,
                    score=0.0,
                    format_ok=False,
                    keys_ok=False,
                    keywords_found=0,
                    response_sec=exec_result.duration_sec,
                    error=exec_result.error,
                )

            results.append(test_result)
            status_icon = "✅" if test_result.passed else "❌"
            logger.info(
                f"  {status_icon} {test.test_id}: score={test_result.score:.2f} "
                f"({test_result.response_sec:.1f}с)"
            )

        # Агрегация результатов
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        avg_score = sum(r.score for r in results) / max(total, 1)
        avg_time = sum(r.response_sec for r in results) / max(total, 1)

        # Сравнение с текущей активной версией
        comparison = None
        active = self.bank.get_active(role)
        if active and active.metrics:
            comparison = {
                "active_version": active.version,
                "active_avg_score": active.metrics.get("avg_score", 0),
                "candidate_avg_score": avg_score,
                "improvement": avg_score - active.metrics.get("avg_score", 0),
            }

        # Вердикт
        if passed / max(total, 1) >= self.min_pass_rate and avg_score >= self.min_avg_score:
            verdict = "passed"
            self.bank.update_status(
                role, version, GenomeStatus.APPROVED,
                test_results={"pass_rate": passed / total, "details": [r.to_dict() for r in results]},
                metrics={"avg_score": avg_score, "avg_response_sec": avg_time, "pass_rate": passed / total},
            )
            logger.info(f"✅ Геном {genome.genome_id} ПРОШЁЛ валидацию ({passed}/{total}, score={avg_score:.2f})")
        else:
            verdict = "failed"
            self.bank.update_status(
                role, version, GenomeStatus.REJECTED,
                test_results={"pass_rate": passed / total, "details": [r.to_dict() for r in results]},
                metrics={"avg_score": avg_score, "avg_response_sec": avg_time, "pass_rate": passed / total},
            )
            logger.warning(f"❌ Геном {genome.genome_id} ПРОВАЛИЛ валидацию ({passed}/{total}, score={avg_score:.2f})")

        return ValidationReport(
            genome_id=genome.genome_id,
            role=role,
            version=version,
            total_tests=total,
            passed_tests=passed,
            avg_score=avg_score,
            avg_response_sec=avg_time,
            test_results=[r.to_dict() for r in results],
            verdict=verdict,
            comparison=comparison,
        )

    async def peresmenka(self, role: str, version: str) -> bool:
        """
        Алгоритм «Пересменка» — валидация и автоматический промоушн.

        Если кандидат проходит тесты и лучше текущей версии:
        1. Approve кандидата
        2. Переключить ЖКХ на новую версию
        3. Архивировать старую
        """
        report = await self.validate_genome(role, version)

        if report.verdict != "passed":
            logger.info(f"Пересменка отклонена: {report.verdict}")
            return False

        # Промоутим
        success = self.bank.promote(role, version)
        if success:
            logger.info(f"🔄 Пересменка завершена: {role}@{version} теперь ACTIVE")
            # Переключаем ЖКХ на новую роль
            try:
                worker_role = WorkerRole(role)
                await self.executor.switch_role(worker_role)
            except ValueError:
                pass
        return success
