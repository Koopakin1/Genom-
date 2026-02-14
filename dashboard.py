#!/usr/bin/env python3
"""
Dashboard API — JSON API + REST для веб-панели мониторинга ГЕНОМ.

Эндпоинты:
    GET  /api/status         — Системные метрики (CPU/RAM/Temp/GPU)
    GET  /api/queues         — Размер очередей Redis
    GET  /api/logs           — Логи задач, решений, инцидентов
    GET  /api/models         — Список моделей Ollama
    POST /api/task           — Отправить задачу
    POST /api/memory/search  — Семантический поиск по Archival Memory
    GET  /api/memory/recall  — Последние события Recall Memory
    GET  /api/memory/core    — Core Memory (персона, состояние)
    POST /api/shift          — Выполнить пересменку
    GET  /                   — Веб-панель (static/index.html)

Запуск: python3 dashboard.py
Порт:   8080
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
import logging
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.resource_monitor import take_snapshot
from core.redis_bus import RedisBus, Task, LogStream
from core.memory import MemoryStore, MemoryEntry
from core.shift_manager import ShiftManager
from worker.roles import WorkerRole

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("genome.dashboard")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = 8080

# Shared instances (created once)
_memory_store = MemoryStore()
_shift_manager = ShiftManager()
_loop = None

def _get_loop():
    """Get or create event loop for async operations."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop


def get_gpu_info() -> dict | None:
    """Получить метрики GPU через sysfs (все card*), lspci, Ollama."""
    gpu = {}

    # 1. Определяем имя GPU через lspci
    try:
        out = subprocess.check_output(
            ["lspci"], timeout=5, text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if "VGA" in line and ("AMD" in line or "ATI" in line or "Radeon" in line):
                # Извлекаем модель: "06:00.0 VGA compatible controller: ... [Radeon RX ...]"
                gpu["name"] = line.split(": ", 1)[-1] if ": " in line else "AMD GPU"
                # Укорачиваем для UI
                if "Ellesmere" in gpu["name"]:
                    gpu["name"] = "AMD Radeon RX 470/480/570/580 (Ellesmere)"
                elif len(gpu["name"]) > 60:
                    gpu["name"] = gpu["name"][:60]
                gpu["driver"] = "amdgpu"
                break
    except Exception:
        pass

    # 2. Сканируем все card* для sysfs метрик
    drm_base = "/sys/class/drm"
    card_path = None
    try:
        if os.path.exists(drm_base):
            for card_dir in sorted(os.listdir(drm_base)):
                if not card_dir.startswith("card") or "-" in card_dir:
                    continue
                device_path = os.path.join(drm_base, card_dir, "device")
                vram_path = os.path.join(device_path, "mem_info_vram_used")
                if os.path.exists(vram_path):
                    card_path = device_path
                    break
    except Exception:
        pass

    if card_path:
        # VRAM
        try:
            with open(os.path.join(card_path, "mem_info_vram_used")) as f:
                gpu["vram_used_mb"] = int(f.read().strip()) / (1024 * 1024)
            with open(os.path.join(card_path, "mem_info_vram_total")) as f:
                gpu["vram_total_mb"] = int(f.read().strip()) / (1024 * 1024)
            gpu["vram_percent"] = round(gpu["vram_used_mb"] / gpu["vram_total_mb"] * 100, 1)
        except Exception:
            pass

        # Температура
        try:
            hwmon_base = os.path.join(card_path, "hwmon")
            if os.path.exists(hwmon_base):
                for hwmon in os.listdir(hwmon_base):
                    temp_file = os.path.join(hwmon_base, hwmon, "temp1_input")
                    if os.path.exists(temp_file):
                        with open(temp_file) as f:
                            gpu["temp_celsius"] = int(f.read().strip()) / 1000
                        break
        except Exception:
            pass

        # Частота
        try:
            freq_file = os.path.join(card_path, "pp_dpm_sclk")
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

        # Загрузка GPU
        try:
            busy_file = os.path.join(card_path, "gpu_busy_percent")
            if os.path.exists(busy_file):
                with open(busy_file) as f:
                    gpu["gpu_percent"] = int(f.read().strip())
        except Exception:
            pass

        # Fan
        try:
            hwmon_base = os.path.join(card_path, "hwmon")
            if os.path.exists(hwmon_base):
                for hwmon in os.listdir(hwmon_base):
                    fan_file = os.path.join(hwmon_base, hwmon, "fan1_input")
                    if os.path.exists(fan_file):
                        with open(fan_file) as f:
                            gpu["fan_rpm"] = int(f.read().strip())
                        break
        except Exception:
            pass

    # 3. Ollama — модели на GPU (fallback info)
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:11434/api/ps", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            models = data.get("models", [])
            if models:
                total_vram = sum(m.get("size_vram", 0) for m in models)
                total_size = sum(m.get("size", 0) for m in models)
                gpu["ollama_models_loaded"] = len(models)
                gpu["ollama_total_size_gb"] = round(total_size / 1e9, 1)
                if total_vram > 0:
                    gpu["ollama_vram_gb"] = round(total_vram / 1e9, 1)
                    gpu["gpu_offload"] = True
                else:
                    gpu["gpu_offload"] = False
    except Exception:
        pass

    return gpu if len(gpu) > 0 else None


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
            "/api/memory/core": self._get_memory_core,
            "/api/memory/recall": self._get_memory_recall,
            "/api/shifts": self._get_shifts,
        }
        if path in handlers:
            self._json_response(handlers[path]())
        else:
            if path == "/":
                self.path = "/index.html"
            super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._json_response({"error": "Invalid JSON"}, status=400)
            return

        post_handlers = {
            "/api/task": self._post_task,
            "/api/memory/search": self._post_memory_search,
            "/api/shift": self._post_shift,
        }
        handler = post_handlers.get(path)
        if handler:
            self._json_response(handler(data))
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

    # ---- Memory API ----

    def _get_memory_core(self) -> dict:
        """GET /api/memory/core — Core Memory."""
        try:
            return {"core": _memory_store.core.get_all()}
        except Exception as e:
            return {"error": str(e)}

    def _get_memory_recall(self) -> dict:
        """GET /api/memory/recall — Последние события."""
        try:
            events = _memory_store.recall.get_recent(count=20)
            return {"events": events, "count": len(events)}
        except Exception as e:
            return {"error": str(e), "events": []}

    def _post_memory_search(self, data: dict) -> dict:
        """POST /api/memory/search — Семантический поиск.
        {"query": "проблемы RAM", "n_results": 5, "category": "incident"}
        """
        query = data.get("query", "")
        if not query:
            return {"error": "Missing 'query'"}
        n = min(data.get("n_results", 5), 20)
        category = data.get("category")
        try:
            loop = _get_loop()
            results = loop.run_until_complete(
                _memory_store.search(query, n_results=n, category=category)
            )
            return {"query": query, "results": results, "count": len(results)}
        except Exception as e:
            return {"error": str(e), "results": []}

    # ---- Shift API ----

    def _get_shifts(self) -> dict:
        """GET /api/shifts — История пересменок."""
        return {
            "current_role": _shift_manager.current_role,
            "history": _shift_manager.history,
        }

    def _post_shift(self, data: dict) -> dict:
        """POST /api/shift — Выполнить пересменку.
        {"to_role": "auditor"}
        """
        to_role_name = data.get("to_role", "")
        try:
            to_role = WorkerRole(to_role_name)
        except ValueError:
            roles = [r.value for r in WorkerRole]
            return {"error": f"Invalid role '{to_role_name}'. Available: {roles}"}

        try:
            loop = _get_loop()
            report = loop.run_until_complete(
                _shift_manager.execute_shift(
                    from_role=_shift_manager.current_role,
                    to_role=to_role,
                    handoff_context=data.get("context"),
                )
            )
            return {"success": report.status.value == "completed", **report.to_dict()}
        except Exception as e:
            return {"error": str(e)}


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
