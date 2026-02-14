"""
Watchdog — Аппаратный предохранитель (вне нейросетей).

Чистый Python-скрипт, который мониторит температуру CPU, RAM
и убивает Docker-контейнеры при превышении порогов.
Не зависит от нейросетей — работает как независимый процесс.
"""

from __future__ import annotations

import os
import sys
import time
import logging
import subprocess

import psutil
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("watchdog")

# Пороги
CPU_TEMP_CRITICAL = int(os.getenv("CPU_TEMP_CRITICAL", "85"))
RAM_CRITICAL_PCT = int(os.getenv("RAM_CRITICAL_PCT", "90"))
INTERVAL_SEC = int(os.getenv("WATCHDOG_INTERVAL_SEC", "10"))

# Контейнеры, которые НЕЛЬЗЯ убивать
PROTECTED_CONTAINERS = {"genome-redis", "genome-chromadb"}


def get_cpu_temp() -> float | None:
    """Получить температуру CPU."""
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        for name in ("k10temp", "coretemp", "cpu_thermal"):
            if name in temps and temps[name]:
                return max(r.current for r in temps[name])
        first = next(iter(temps.values()), [])
        return max(r.current for r in first) if first else None
    except Exception:
        return None


def get_genome_containers() -> list[str]:
    """Получить список запущенных genome-* контейнеров."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=genome-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
        return [c.strip() for c in result.stdout.strip().split("\n") if c.strip()]
    except Exception:
        return []


def kill_container(name: str) -> bool:
    """Остановить Docker-контейнер."""
    try:
        subprocess.run(
            ["docker", "stop", "-t", "5", name],
            capture_output=True, timeout=15,
        )
        logger.warning(f"🛑 Контейнер {name} остановлен")
        return True
    except Exception as e:
        logger.error(f"Не удалось остановить {name}: {e}")
        return False


def emergency_action(reason: str) -> None:
    """Аварийное действие: остановить все не-защищённые контейнеры."""
    logger.critical(f"🚨 АВАРИЙНЫЙ РЕЖИМ: {reason}")
    containers = get_genome_containers()
    for c in containers:
        if c not in PROTECTED_CONTAINERS:
            kill_container(c)

    # Попытка убить процессы ollama
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if "ollama" in (proc.info["name"] or "").lower():
                logger.warning(f"🛑 Убиваю процесс Ollama (PID {proc.info['pid']})")
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def run_watchdog() -> None:
    """Главный цикл Watchdog."""
    logger.info("=" * 50)
    logger.info("🐕 WATCHDOG запущен")
    logger.info(f"   Порог температуры: {CPU_TEMP_CRITICAL}°C")
    logger.info(f"   Порог RAM: {RAM_CRITICAL_PCT}%")
    logger.info(f"   Интервал: {INTERVAL_SEC}с")
    logger.info(f"   Защищённые: {PROTECTED_CONTAINERS}")
    logger.info("=" * 50)

    consecutive_critical = 0

    while True:
        try:
            cpu_temp = get_cpu_temp()
            ram = psutil.virtual_memory()

            # Температура
            if cpu_temp is not None and cpu_temp > CPU_TEMP_CRITICAL:
                consecutive_critical += 1
                logger.critical(
                    f"🌡️  CPU: {cpu_temp}°C > {CPU_TEMP_CRITICAL}°C "
                    f"(последовательных: {consecutive_critical})"
                )
                if consecutive_critical >= 3:
                    emergency_action(f"Температура CPU {cpu_temp}°C (3 подряд)")
                    consecutive_critical = 0
            elif ram.percent > RAM_CRITICAL_PCT:
                consecutive_critical += 1
                logger.critical(
                    f"🧠 RAM: {ram.percent}% > {RAM_CRITICAL_PCT}% "
                    f"(последовательных: {consecutive_critical})"
                )
                if consecutive_critical >= 3:
                    emergency_action(f"RAM {ram.percent}% (3 подряд)")
                    consecutive_critical = 0
            else:
                if consecutive_critical > 0:
                    logger.info("✅ Показатели вернулись в норму")
                consecutive_critical = 0

            time.sleep(INTERVAL_SEC)

        except KeyboardInterrupt:
            logger.info("Watchdog остановлен")
            break
        except Exception as e:
            logger.error(f"Ошибка Watchdog: {e}")
            time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    run_watchdog()
