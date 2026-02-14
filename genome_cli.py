#!/usr/bin/env python3
"""
genome-cli — Утилита управления ИИ-Полисом «ГЕНОМ».

Использование:
    python3 -m genome_cli task <type> <payload>   — Отправить задачу
    python3 -m genome_cli status                  — Статус системы
    python3 -m genome_cli queues                  — Размер очередей
    python3 -m genome_cli logs [stream] [count]   — Просмотр логов
    python3 -m genome_cli analyze <file>          — Статический анализ кода
    python3 -m genome_cli validate <role> <ver>   — Запустить «Пересменку»
"""

from __future__ import annotations

import sys
import os
import json
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.redis_bus import RedisBus, Task, QueuePriority, LogStream


def cmd_task(args: list[str]) -> None:
    """Отправить задачу в очередь."""
    if len(args) < 1:
        print("Использование: genome-cli task <type> [payload_json] [priority]")
        print("  priority: critical, export (default), internal")
        return

    task_type = args[0]
    payload = json.loads(args[1]) if len(args) > 1 else {"message": "manual task"}
    priority = args[2] if len(args) > 2 else "export"

    bus = RedisBus()
    if not bus.ping():
        print("❌ Redis недоступен!")
        return

    task = Task(
        task_id=f"manual_{uuid.uuid4().hex[:8]}",
        task_type=task_type,
        payload=payload,
        priority=priority,
        source="cli",
    )
    bus.push_task(task)
    print(f"✅ Задача отправлена: {task.task_id} (тип: {task_type}, приоритет: {priority})")
    bus.close()


def cmd_status() -> None:
    """Показать статус системы."""
    bus = RedisBus()
    if not bus.ping():
        print("❌ Redis недоступен!")
        return

    from core.resource_monitor import take_snapshot
    snapshot = take_snapshot()

    worker_status = bus.get_state(bus.__class__.__mro__[0].__module__ and
                                  type('', (), {'value': 'STATE:WORKER:STATUS'})()) or "unknown"

    print("=" * 50)
    print("🏙️  ИИ-ПОЛИС «ГЕНОМ» — СТАТУС")
    print("=" * 50)
    print(f"  💻 CPU: {snapshot.cpu_percent}%")
    print(f"  🧠 RAM: {snapshot.ram_used_mb:.0f}/{snapshot.ram_total_mb:.0f} МБ ({snapshot.ram_percent}%)")
    print(f"  💾 Disk: {snapshot.disk_used_gb:.1f}/{snapshot.disk_total_gb:.1f} ГБ")
    if snapshot.cpu_temp_celsius:
        print(f"  🌡️  Temp: {snapshot.cpu_temp_celsius}°C")
    print(f"  ⚠️  Critical: {snapshot.is_critical}")
    print()

    # Очереди
    lengths = bus.queue_lengths()
    print("📬 Очереди:")
    for name, length in lengths.items():
        print(f"  {name}: {length}")
    print()
    bus.close()


def cmd_queues() -> None:
    """Показать размеры очередей."""
    bus = RedisBus()
    if not bus.ping():
        print("❌ Redis недоступен!")
        return
    lengths = bus.queue_lengths()
    for name, length in lengths.items():
        print(f"{name}: {length}")
    bus.close()


def cmd_logs(args: list[str]) -> None:
    """Показать логи."""
    stream_name = args[0] if args else "DECISIONS"
    count = int(args[1]) if len(args) > 1 else 10

    stream_map = {
        "DECISIONS": LogStream.DECISIONS,
        "TASKS": LogStream.TASKS,
        "INCIDENTS": LogStream.INCIDENTS,
    }

    stream = stream_map.get(stream_name.upper())
    if not stream:
        print(f"Доступные потоки: {', '.join(stream_map.keys())}")
        return

    bus = RedisBus()
    if not bus.ping():
        print("❌ Redis недоступен!")
        return

    entries = bus.read_log(stream, count=count)
    if not entries:
        print(f"Лог {stream_name} пуст.")
    else:
        for entry in entries:
            print(json.dumps(entry, indent=2, ensure_ascii=False, default=str))
            print("---")
    bus.close()


def cmd_analyze(args: list[str]) -> None:
    """Статический анализ файла."""
    if not args:
        print("Использование: genome-cli analyze <file.py>")
        return

    filepath = args[0]
    if not os.path.exists(filepath):
        print(f"❌ Файл не найден: {filepath}")
        return

    from security.static_analysis import analyze_code
    with open(filepath) as f:
        code = f.read()

    report = analyze_code(code)
    print(f"{'✅ БЕЗОПАСНО' if report.safe else '⚠️ ОПАСНО'} (risk: {report.risk_level}/10)")
    print(report.summary)
    if report.findings:
        print()
        for f in report.findings:
            icon = "🔴" if f.severity == "critical" else "🟡" if f.severity == "high" else "🟢"
            print(f"  {icon} [{f.severity}] строка {f.line_number}: {f.description}")
            print(f"     {f.code_snippet}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    command = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "task": lambda: cmd_task(args),
        "status": cmd_status,
        "queues": cmd_queues,
        "logs": lambda: cmd_logs(args),
        "analyze": lambda: cmd_analyze(args),
    }

    handler = commands.get(command)
    if handler:
        handler()
    else:
        print(f"Неизвестная команда: {command}")
        print(__doc__)


if __name__ == "__main__":
    main()
