"""
Orchestrator — Мозг Администрации ИИ-Полиса «ГЕНОМ».

Главный управляющий цикл:
1. Читает очереди Redis (CRITICAL → EXPORT → INTERNAL)
2. Оценивает стоимость задач через qwen2.5:1.5b
3. Проверяет ресурсы
4. Назначает роль ЖКХ и отправляет на исполнение
5. Валидирует результаты
6. Логирует решения в Redis Streams
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

from core.redis_bus import RedisBus, Task, LogStream
from core.resource_monitor import take_snapshot
from core.memory import MemoryStore, MemoryEntry
from worker.executor import WorkerExecutor
from worker.roles import WorkerRole
from security.static_analysis import analyze_code

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("genome.orchestrator")

OLLAMA_URL = "http://localhost:11434"
ADMIN_MODEL = "qwen2.5:1.5b"
POLL_INTERVAL_SEC = 5


class Orchestrator:
    """Администрация — мозг ИИ-Полиса."""

    def __init__(self):
        self.bus = RedisBus()
        self.memory = MemoryStore()
        self.executor = WorkerExecutor()
        self._running = False
        self._cycle_count = 0
        self._budget = 1000.0  # Стартовый бюджет в Юнитах

    async def start(self) -> None:
        """Запуск главного цикла Администрации."""
        logger.info("=" * 60)
        logger.info("🏙️  ИИ-ПОЛИС «ГЕНОМ» — АДМИНИСТРАЦИЯ ЗАПУЩЕНА")
        logger.info("=" * 60)

        if not self.bus.ping():
            logger.error("❌ Redis недоступен! Невозможно запустить.")
            return

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{OLLAMA_URL}/")
                if resp.status_code == 200:
                    logger.info("✅ Ollama: подключён")
        except Exception:
            logger.error("❌ Ollama недоступен!")
            return

        # Инициализация памяти
        try:
            await self.memory.initialize()
            logger.info("✅ ChromaDB: память инициализирована")
        except Exception as e:
            logger.warning(f"⚠️ ChromaDB: {e} (работаем без памяти)")

        snapshot = take_snapshot()
        logger.info(f"💻 CPU: {snapshot.cpu_percent}% | 🧠 RAM: {snapshot.ram_percent}%")
        logger.info(f"💰 Бюджет: {self._budget} Юнитов")
        logger.info(f"📬 Очереди: {self.bus.queue_lengths()}")
        logger.info("")

        self._running = True
        self.bus.log(LogStream.DECISIONS, {
            "event": "orchestrator_start",
            "budget": self._budget,
        })

        try:
            while self._running:
                await self._cycle()
                await asyncio.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            logger.info("🛑 Остановка по Ctrl+C")
        except Exception as e:
            logger.error(f"💥 Критическая ошибка: {e}")
            self.bus.log(LogStream.INCIDENTS, {
                "event": "orchestrator_crash", "error": str(e),
            })
        finally:
            self._running = False
            self.bus.close()
            logger.info("Администрация остановлена.")

    async def _cycle(self) -> None:
        """Один цикл работы Администрации."""
        self._cycle_count += 1

        snapshot = take_snapshot()
        if snapshot.is_critical:
            logger.warning("⚠️ Критическое состояние ресурсов! Пропускаю цикл.")
            return

        task = self.bus.pop_task()
        if not task:
            if self._cycle_count % 12 == 0:
                logger.info(f"💤 Очереди пусты (цикл #{self._cycle_count})")
            return

        logger.info(f"📋 Задача: {task.task_id} (тип: {task.task_type}, приоритет: {task.priority})")

        cost = await self._estimate_cost(task)
        if self._budget < cost:
            logger.warning(f"💸 Бюджет: {self._budget:.1f} < {cost:.1f}")
            self.bus.push_task(task)
            return

        role = self._select_role(task)
        logger.info(f"🔧 Роль: {role.value} | 💰 Стоимость: {cost:.1f} Юнитов")

        # Проверка безопасности кода
        if "code" in task.payload:
            report = analyze_code(task.payload["code"])
            if not report.safe:
                logger.warning(f"🛑 Код ЗАБЛОКИРОВАН: {report.summary}")
                self.bus.log(LogStream.INCIDENTS, {
                    "event": "code_blocked",
                    "task_id": task.task_id,
                    "risk_level": report.risk_level,
                })
                return

        prompt = self._build_prompt(task)
        result = await self.executor.execute(
            task_id=task.task_id, prompt=prompt, role=role,
        )

        if result.success:
            self._budget -= cost
            logger.info(f"✅ Выполнено за {result.duration_sec:.1f}с | Остаток: {self._budget:.1f}")

            try:
                await self.memory.store(MemoryEntry(
                    content=result.output[:500],
                    category="task_result",
                    metadata={"task_id": task.task_id, "role": role.value},
                ))
            except Exception:
                pass  # Память не критична

            self.bus.log(LogStream.TASKS, {
                "event": "task_completed",
                "task_id": task.task_id,
                "role": role.value,
                "cost": cost,
                "duration_sec": result.duration_sec,
            })
        else:
            logger.error(f"❌ Ошибка: {result.error}")
            self.bus.log(LogStream.TASKS, {
                "event": "task_failed",
                "task_id": task.task_id,
                "error": result.error,
            })

    async def _estimate_cost(self, task: Task) -> float:
        """Оценить стоимость через qwen."""
        try:
            prompt = (
                f"Оцени стоимость задачи в Юнитах (1-100). "
                f"Тип: {task.task_type}. "
                f"Данные: {json.dumps(task.payload, ensure_ascii=False)[:200]}. "
                f"Ответь ТОЛЬКО числом."
            )
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={"model": ADMIN_MODEL, "prompt": prompt, "stream": False},
                )
                if resp.status_code == 200:
                    text = resp.json().get("response", "5")
                    numbers = re.findall(r"\d+(?:\.\d+)?", text)
                    return float(numbers[0]) if numbers else 5.0
        except Exception:
            pass
        return 5.0

    def _select_role(self, task: Task) -> WorkerRole:
        """Выбрать роль ЖКХ."""
        t = task.task_type.lower()
        mapping = {
            "sysadmin": WorkerRole.SYSADMIN, "docker": WorkerRole.SYSADMIN,
            "system": WorkerRole.SYSADMIN, "audit": WorkerRole.AUDITOR,
            "security": WorkerRole.AUDITOR, "review": WorkerRole.AUDITOR,
            "economy": WorkerRole.ECONOMIST, "cost": WorkerRole.ECONOMIST,
            "clean": WorkerRole.CLEANER, "garbage": WorkerRole.CLEANER,
            "emergency": WorkerRole.MCHS, "mchs": WorkerRole.MCHS,
        }
        for kw, role in mapping.items():
            if kw in t:
                return role
        return WorkerRole.SYSADMIN

    def _build_prompt(self, task: Task) -> str:
        """Собрать промпт для ЖКХ."""
        payload_str = json.dumps(task.payload, ensure_ascii=False, indent=2)
        return (
            f"Задача #{task.task_id}\n"
            f"Тип: {task.task_type}\nПриоритет: {task.priority}\n"
            f"Данные:\n{payload_str}\n\n"
            f"Выполни задачу и верни JSON: "
            f'{{"status":"ok","actions_taken":[...],"output":"..."}}'
        )

    def stop(self):
        self._running = False


async def main():
    orchestrator = Orchestrator()
    await orchestrator.start()


if __name__ == "__main__":
    asyncio.run(main())
