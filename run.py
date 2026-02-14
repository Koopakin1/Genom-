#!/usr/bin/env python3
"""
run.py — Единая точка входа для ГЕНОМ.

Режимы:
    python3 run.py               # Оркестратор (по умолчанию)
    python3 run.py --watchdog    # Watchdog-предохранитель
    python3 run.py --dashboard   # Веб-дэшборд (порт 8080)
    python3 run.py --scheduler   # Планировщик автозадач
    python3 run.py --notifier    # Telegram-уведомления
    python3 run.py --all         # Все компоненты в одном процессе
"""

import os
import sys
import asyncio
import argparse
import threading

# Гарантируем PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run_orchestrator():
    """Запустить Оркестратор."""
    from core.orchestrator import Orchestrator
    orch = Orchestrator()
    asyncio.run(orch.start())


def run_watchdog():
    """Запустить Watchdog."""
    from security.watchdog import run_watchdog as _run_watchdog
    _run_watchdog()


def run_dashboard():
    """Запустить Dashboard."""
    from dashboard import DashboardHandler, STATIC_DIR, PORT
    from http.server import HTTPServer
    import logging
    logger = logging.getLogger("genome.dashboard")
    os.makedirs(STATIC_DIR, exist_ok=True)
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    logger.info(f"🖥️  Дэшборд ГЕНОМ запущен: http://localhost:{PORT}")
    server.serve_forever()


def run_scheduler():
    """Запустить Scheduler."""
    from scheduler import Scheduler
    sched = Scheduler()
    sched.start()


def run_notifier():
    """Запустить Notifier."""
    from notifier import Notifier
    notif = Notifier()
    notif.start()


def run_all():
    """Запустить все компоненты в потоках."""
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("genome.main")
    logger.info("🚀 Запуск ВСЕХ компонентов ГЕНОМ...")

    daemons = [
        ("watchdog", run_watchdog),
        ("dashboard", run_dashboard),
        ("scheduler", run_scheduler),
        ("notifier", run_notifier),
    ]

    for name, func in daemons:
        t = threading.Thread(target=func, daemon=True, name=name)
        t.start()
        icons = {"watchdog": "🐕", "dashboard": "🖥️", "scheduler": "⏰", "notifier": "🔔"}
        logger.info(f"{icons.get(name, '▶')}  {name} запущен")

    # Оркестратор — главный поток
    run_orchestrator()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ГЕНОМ — Автономный ИИ-Полис")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--watchdog", action="store_true", help="Запустить Watchdog")
    group.add_argument("--dashboard", action="store_true", help="Запустить Dashboard")
    group.add_argument("--scheduler", action="store_true", help="Запустить Scheduler")
    group.add_argument("--notifier", action="store_true", help="Запустить Notifier")
    group.add_argument("--all", action="store_true", help="Все компоненты")
    args = parser.parse_args()

    runners = {
        "watchdog": run_watchdog,
        "dashboard": run_dashboard,
        "scheduler": run_scheduler,
        "notifier": run_notifier,
    }

    for name, func in runners.items():
        if getattr(args, name, False):
            func()
            sys.exit(0)

    if args.all:
        run_all()
    else:
        run_orchestrator()
