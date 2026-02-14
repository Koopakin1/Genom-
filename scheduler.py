#!/usr/bin/env python3
"""
Scheduler — Планировщик автоматических задач ГЕНОМ.

Периодически отправляет задачи в Redis для:
- Health check системы (каждые 5 минут)
- Анализ логов и очистка (каждый час)
- Ежедневный отчёт (раз в сутки)
- Мониторинг безопасности (каждые 30 минут)

Запуск: python3 scheduler.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import logging
import threading
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.redis_bus import RedisBus, Task
from core.resource_monitor import take_snapshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("genome.scheduler")


@dataclass
class ScheduledJob:
    """Определение периодической задачи."""
    name: str
    task_type: str
    payload: dict
    interval_sec: int
    priority: str = "internal"
    enabled: bool = True
    last_run: float = 0
    run_count: int = 0


# Реестр автоматических задач
JOBS: list[ScheduledJob] = [
    ScheduledJob(
        name="system_health_check",
        task_type="sysadmin",
        payload={
            "action": "health_check",
            "checks": ["docker_containers", "disk_space", "memory_usage", "network"],
            "auto": True,
        },
        interval_sec=300,  # 5 минут
        priority="internal",
    ),
    ScheduledJob(
        name="security_scan",
        task_type="auditor",
        payload={
            "action": "periodic_security_scan",
            "targets": ["genome_codebase", "docker_configs", "exposed_ports"],
            "auto": True,
        },
        interval_sec=1800,  # 30 минут
        priority="export",
    ),
    ScheduledJob(
        name="log_cleanup",
        task_type="cleaner",
        payload={
            "action": "cleanup",
            "targets": ["old_logs", "docker_images", "temp_files", "redis_old_entries"],
            "auto": True,
            "max_age_hours": 24,
        },
        interval_sec=3600,  # 1 час
        priority="internal",
    ),
    ScheduledJob(
        name="resource_forecast",
        task_type="economist",
        payload={
            "action": "resource_forecast",
            "analyze": ["cpu_trend", "ram_trend", "disk_growth", "budget_usage"],
            "auto": True,
        },
        interval_sec=3600,  # 1 час
        priority="internal",
    ),
    ScheduledJob(
        name="daily_report",
        task_type="auditor",
        payload={
            "action": "daily_report",
            "include": [
                "tasks_completed", "tasks_failed", "resource_usage",
                "security_incidents", "budget_status", "model_usage",
            ],
            "auto": True,
        },
        interval_sec=86400,  # 24 часа
        priority="export",
    ),
]


class Scheduler:
    """Планировщик автоматических задач."""

    def __init__(self):
        self.bus = RedisBus()
        self._running = False

    def start(self):
        """Запуск цикла планировщика."""
        logger.info("=" * 50)
        logger.info("⏰ SCHEDULER запущен")
        logger.info(f"   Задач: {len([j for j in JOBS if j.enabled])}")
        for job in JOBS:
            if job.enabled:
                m = job.interval_sec // 60
                logger.info(f"   📋 {job.name}: каждые {m}мин ({job.task_type})")
        logger.info("=" * 50)

        if not self.bus.ping():
            logger.error("❌ Redis недоступен!")
            return

        self._running = True

        try:
            while self._running:
                self._check_jobs()
                time.sleep(10)  # Проверяем каждые 10 секунд
        except KeyboardInterrupt:
            logger.info("Scheduler остановлен.")
        finally:
            self.bus.close()

    def _check_jobs(self):
        """Проверить и запустить задачи по расписанию."""
        now = time.time()
        snapshot = take_snapshot()

        # Не запускаем задачи если система перегружена
        if snapshot.is_critical:
            logger.warning("⚠️ Система в критическом состоянии — автозадачи приостановлены")
            return

        for job in JOBS:
            if not job.enabled:
                continue

            elapsed = now - job.last_run
            if elapsed >= job.interval_sec:
                self._submit_job(job, snapshot)
                job.last_run = now
                job.run_count += 1

    def _submit_job(self, job: ScheduledJob, snapshot):
        """Отправить задачу в Redis."""
        task_id = f"auto_{job.name}_{int(time.time())}"

        # Обогащаем payload контекстом
        enriched_payload = {
            **job.payload,
            "scheduler_context": {
                "cpu_percent": snapshot.cpu_percent,
                "ram_percent": snapshot.ram_percent,
                "disk_percent": snapshot.disk_percent,
                "run_number": job.run_count + 1,
            },
        }

        task = Task(
            task_id=task_id,
            task_type=job.task_type,
            payload=enriched_payload,
            priority=job.priority,
            source="scheduler",
        )

        try:
            self.bus.push_task(task)
            logger.info(
                f"⏰ [{job.name}] → {task_id} "
                f"(очередь: {job.priority.upper()}, #{job.run_count + 1})"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки {job.name}: {e}")

    def stop(self):
        self._running = False


if __name__ == "__main__":
    scheduler = Scheduler()
    scheduler.start()
