#!/usr/bin/env python3
"""
Точка входа для запуска Администрации ИИ-Полиса «ГЕНОМ».

Использование:
    python3 run.py              — запуск Администрации
    python3 run.py --watchdog   — запуск Watchdog
"""

import sys
import os
import asyncio

# Добавляем genome/ в PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--watchdog":
        from security.watchdog import Watchdog
        print("🔒 Запуск Watchdog...")
        wd = Watchdog()
        try:
            wd.start()
        except KeyboardInterrupt:
            print("Watchdog остановлен.")
    else:
        from core.orchestrator import main as orchestrator_main
        asyncio.run(orchestrator_main())


if __name__ == "__main__":
    main()
