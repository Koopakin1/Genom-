"""
Resource Monitor — Мониторинг аппаратных ресурсов.

Сбор данных о CPU, RAM, температуре, дисковом пространстве.
Используется Администрацией и Watchdog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psutil

logger = logging.getLogger("genome.resource_monitor")


@dataclass
class SystemSnapshot:
    """Снимок состояния системы."""
    cpu_percent: float          # % загрузки CPU
    cpu_freq_mhz: float         # Частота CPU в МГц
    ram_total_mb: float         # Всего RAM
    ram_used_mb: float          # Используется RAM
    ram_available_mb: float     # Доступно RAM
    ram_percent: float          # % использования RAM
    disk_total_gb: float        # Всего диск
    disk_used_gb: float         # Использовано диск
    disk_free_gb: float         # Свободно диск
    disk_percent: float         # % использования диска
    cpu_temp_celsius: float | None  # Температура CPU (может быть None)
    load_avg_1m: float          # Средняя нагрузка за 1 мин
    load_avg_5m: float          # Средняя нагрузка за 5 мин
    load_avg_15m: float         # Средняя нагрузка за 15 мин

    @property
    def is_critical(self) -> bool:
        """Критическое состояние: RAM > 90% или температура > 85°C."""
        if self.ram_percent > 90:
            return True
        if self.cpu_temp_celsius is not None and self.cpu_temp_celsius > 85:
            return True
        return False

    @property
    def is_warning(self) -> bool:
        """Предупреждение: RAM > 80% или температура > 75°C."""
        if self.ram_percent > 80:
            return True
        if self.cpu_temp_celsius is not None and self.cpu_temp_celsius > 75:
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "cpu_percent": self.cpu_percent,
            "cpu_freq_mhz": self.cpu_freq_mhz,
            "ram_total_mb": self.ram_total_mb,
            "ram_used_mb": self.ram_used_mb,
            "ram_available_mb": self.ram_available_mb,
            "ram_percent": self.ram_percent,
            "disk_total_gb": self.disk_total_gb,
            "disk_used_gb": self.disk_used_gb,
            "disk_free_gb": self.disk_free_gb,
            "disk_percent": self.disk_percent,
            "cpu_temp_celsius": self.cpu_temp_celsius,
            "load_avg_1m": self.load_avg_1m,
            "load_avg_5m": self.load_avg_5m,
            "load_avg_15m": self.load_avg_15m,
            "is_critical": self.is_critical,
            "is_warning": self.is_warning,
        }


def get_cpu_temp() -> float | None:
    """Получить температуру CPU. Возвращает None если недоступна."""
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        # Ищем температуру в порядке приоритета
        for name in ("k10temp", "coretemp", "cpu_thermal", "acpitz"):
            if name in temps:
                readings = temps[name]
                if readings:
                    return max(r.current for r in readings)
        # Берём первую доступную
        first_group = next(iter(temps.values()))
        if first_group:
            return max(r.current for r in first_group)
    except Exception as e:
        logger.warning(f"Не удалось получить температуру CPU: {e}")
    return None


def take_snapshot() -> SystemSnapshot:
    """Сделать полный снимок состояния системы."""
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_freq = psutil.cpu_freq()
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load = psutil.getloadavg()

    return SystemSnapshot(
        cpu_percent=cpu_percent,
        cpu_freq_mhz=cpu_freq.current if cpu_freq else 0,
        ram_total_mb=ram.total / (1024 ** 2),
        ram_used_mb=ram.used / (1024 ** 2),
        ram_available_mb=ram.available / (1024 ** 2),
        ram_percent=ram.percent,
        disk_total_gb=disk.total / (1024 ** 3),
        disk_used_gb=disk.used / (1024 ** 3),
        disk_free_gb=disk.free / (1024 ** 3),
        disk_percent=disk.percent,
        cpu_temp_celsius=get_cpu_temp(),
        load_avg_1m=load[0],
        load_avg_5m=load[1],
        load_avg_15m=load[2],
    )
