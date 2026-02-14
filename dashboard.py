#!/usr/bin/env python3
"""
Dashboard API — JSON API + REST для веб-панели мониторинга ГЕНОМ.

Эндпоинты:
    GET  /api/status    — Системные метрики (CPU/RAM/Temp/GPU)
    GET  /api/queues    — Размер очередей Redis
    GET  /api/logs      — Логи задач, решений, инцидентов
    GET  /api/models    — Список моделей Ollama
    POST /api/task      — Отправить задачу (REST API для НИИ)
    GET  /               — Веб-панель (static/index.html)

Запуск: python3 dashboard.py
Порт:   8080
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
import logging
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.resource_monitor import take_snapshot
from core.redis_bus import RedisBus, Task, LogStream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("genome.dashboard")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = 8080


def get_gpu_info() -> dict | None:
    """Получить метрики AMD RX 560 через sysfs и radeontop."""
    gpu = {"name": "AMD RX 560", "driver": "amdgpu"}

    # VRAM через sysfs
    try:
        vram_used_path = "/sys/class/drm/card0/device/mem_info_vram_used"
        vram_total_path = "/sys/class/drm/card0/device/mem_info_vram_total"

        if os.path.exists(vram_used_path) and os.path.exists(vram_total_path):
            with open(vram_used_path) as f:
                gpu["vram_used_mb"] = int(f.read().strip()) / (1024 * 1024)
            with open(vram_total_path) as f:
                gpu["vram_total_mb"] = int(f.read().strip()) / (1024 * 1024)
            gpu["vram_percent"] = round(gpu["vram_used_mb"] / gpu["vram_total_mb"] * 100, 1)
    except Exception:
        pass

    # Температура GPU через hwmon
    try:
        hwmon_base = "/sys/class/drm/card0/device/hwmon/"
        if os.path.exists(hwmon_base):
            for hwmon in os.listdir(hwmon_base):
                temp_file = os.path.join(hwmon_base, hwmon, "temp1_input")
                if os.path.exists(temp_file):
                    with open(temp_file) as f:
                        gpu["temp_celsius"] = int(f.read().strip()) / 1000
                    break
    except Exception:
        pass

    # Частота GPU
    try:
        freq_file = "/sys/class/drm/card0/device/pp_dpm_sclk"
        if os.path.exists(freq_file):
            with open(freq_file) as f:
                for line in f:
                    if "*" in line:
                        match = re.search(r"(\d+)Mhz", line)
                        if match:
                            gpu["freq_mhz"] = int(match.group(1))
                        break
    except Exception:
        pass

    # Загрузка GPU через /sys/class/drm/card0/device/gpu_busy_percent
    try:
        busy_file = "/sys/class/drm/card0/device/gpu_busy_percent"
        if os.path.exists(busy_file):
            with open(busy_file) as f:
                gpu["gpu_percent"] = int(f.read().strip())
    except Exception:
        pass

    # Fan speed
    try:
        hwmon_base = "/sys/class/drm/card0/device/hwmon/"
        if os.path.exists(hwmon_base):
            for hwmon in os.listdir(hwmon_base):
                fan_file = os.path.join(hwmon_base, hwmon, "fan1_input")
                if os.path.exists(fan_file):
                    with open(fan_file) as f:
                        gpu["fan_rpm"] = int(f.read().strip())
                    break
    except Exception:
        pass

    return gpu if len(gpu) > 2 else None


class DashboardHandler(SimpleHTTPRequestHandler):
    """Обработчик: статика + JSON API + REST."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path
        handlers = {
            "/api/status": self._get_status,
            "/api/queues": self._get_queues,
            "/api/logs": self._get_logs,
            "/api/models": self._get_models,
        }
        if path in handlers:
            self._json_response(handlers[path]())
        else:
            if path == "/":
                self.path = "/index.html"
            super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/task":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._json_response({"error": "Invalid JSON"}, status=400)
                return
            self._json_response(self._post_task(data))
        else:
            self._json_response({"error": "Not found"}, status=404)

    def _json_response(self, data: dict | list, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _get_status(self) -> dict:
        snapshot = take_snapshot()
        result = {
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
        gpu = get_gpu_info()
        if gpu:
            result["gpu"] = gpu
        return result

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

    def _post_task(self, data: dict) -> dict:
        """REST API: отправить задачу.

        POST /api/task
        {
            "type": "sysadmin",
            "payload": {"action": "check_docker"},
            "priority": "export"  // optional: critical, export, internal
        }
        """
        task_type = data.get("type")
        if not task_type:
            return {"error": "Missing 'type' field", "example": {
                "type": "sysadmin",
                "payload": {"action": "check_docker"},
                "priority": "export",
            }}

        payload = data.get("payload", {"message": "API task"})
        priority = data.get("priority", "export")
        task_id = f"api_{uuid.uuid4().hex[:8]}"

        try:
            bus = RedisBus()
            if not bus.ping():
                return {"error": "Redis unavailable"}

            task = Task(
                task_id=task_id,
                task_type=task_type,
                payload=payload,
                priority=priority,
                source="rest_api",
            )
            bus.push_task(task)
            bus.close()
            return {
                "success": True,
                "task_id": task_id,
                "type": task_type,
                "priority": priority,
                "message": f"Task {task_id} queued",
            }
        except Exception as e:
            return {"error": str(e)}

    def log_message(self, format, *args):
        if "/api/" not in str(args):
            super().log_message(format, *args)


if __name__ == "__main__":
    os.makedirs(STATIC_DIR, exist_ok=True)
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    logger.info(f"🖥️  Дэшборд ГЕНОМ запущен: http://localhost:{PORT}")
    logger.info(f"📡 REST API: POST http://localhost:{PORT}/api/task")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Дэшборд остановлен.")
        server.server_close()
