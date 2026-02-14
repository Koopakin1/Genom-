#!/usr/bin/env python3
"""
Dashboard API — JSON API для веб-панели мониторинга ГЕНОМ.

Запуск: python3 dashboard.py
Порт: 8080
"""

from __future__ import annotations

import json
import os
import sys
import time
import logging
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.resource_monitor import take_snapshot
from core.redis_bus import RedisBus, LogStream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("genome.dashboard")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = 8080


class DashboardHandler(SimpleHTTPRequestHandler):
    """Обработчик для дэшборда: статика + JSON API."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/status":
            self._json_response(self._get_status())
        elif path == "/api/queues":
            self._json_response(self._get_queues())
        elif path == "/api/logs":
            self._json_response(self._get_logs())
        elif path == "/api/models":
            self._json_response(self._get_models())
        else:
            # Статика
            if path == "/":
                self.path = "/index.html"
            super().do_GET()

    def _json_response(self, data: dict | list):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_status(self) -> dict:
        snapshot = take_snapshot()
        return {
            "cpu_percent": snapshot.cpu_percent,
            "cpu_freq_mhz": snapshot.cpu_freq_mhz,
            "ram_total_mb": round(snapshot.ram_total_mb),
            "ram_used_mb": round(snapshot.ram_used_mb),
            "ram_percent": snapshot.ram_percent,
            "disk_total_gb": round(snapshot.disk_total_gb, 1),
            "disk_used_gb": round(snapshot.disk_used_gb, 1),
            "disk_percent": snapshot.disk_percent,
            "cpu_temp": snapshot.cpu_temp_celsius,
            "load_avg": [snapshot.load_avg_1m, snapshot.load_avg_5m, snapshot.load_avg_15m],
            "is_critical": snapshot.is_critical,
            "is_warning": snapshot.is_warning,
            "timestamp": time.time(),
        }

    def _get_queues(self) -> dict:
        try:
            bus = RedisBus()
            if bus.ping():
                lengths = bus.queue_lengths()
                bus.close()
                return {"connected": True, "queues": lengths}
        except Exception:
            pass
        return {"connected": False, "queues": {}}

    def _get_logs(self) -> dict:
        try:
            bus = RedisBus()
            if not bus.ping():
                return {"tasks": [], "decisions": [], "incidents": []}
            tasks = bus.read_log(LogStream.TASKS, count=20)
            decisions = bus.read_log(LogStream.DECISIONS, count=10)
            incidents = bus.read_log(LogStream.INCIDENTS, count=10)
            bus.close()
            return {"tasks": tasks, "decisions": decisions, "incidents": incidents}
        except Exception:
            return {"tasks": [], "decisions": [], "incidents": []}

    def _get_models(self) -> dict:
        try:
            import httpx
            resp = httpx.get("http://localhost:11434/api/tags", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                models = [
                    {"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1)}
                    for m in data.get("models", [])
                ]
                return {"connected": True, "models": models}
        except Exception:
            pass
        return {"connected": False, "models": []}

    def log_message(self, format, *args):
        # Подавляем логи GET-запросов для /api/ (слишком шумно)
        if "/api/" not in str(args):
            super().log_message(format, *args)


if __name__ == "__main__":
    os.makedirs(STATIC_DIR, exist_ok=True)
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    logger.info(f"🖥️  Дэшборд ГЕНОМ запущен: http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Дэшборд остановлен.")
        server.server_close()
