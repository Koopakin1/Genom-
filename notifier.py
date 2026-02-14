#!/usr/bin/env python3
"""
Notifier — Система уведомлений ГЕНОМ.

Слушает Redis Pub/Sub + Streams и отправляет уведомления:
- Telegram (через Bot API)
- Лог-файл (всегда)

Конфигурация через .env:
    TELEGRAM_BOT_TOKEN=ваш_токен
    TELEGRAM_CHAT_ID=ваш_chat_id

Запуск: python3 notifier.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass
from enum import Enum

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.redis_bus import RedisBus, LogStream

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("genome.notifier")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


class Severity(str, Enum):
    INFO = "info"         # Обычные уведомления
    WARNING = "warning"   # Предупреждения
    CRITICAL = "critical" # Критические инциденты
    SUCCESS = "success"   # Успешные операции


@dataclass
class Notification:
    """Уведомление для отправки."""
    title: str
    message: str
    severity: Severity = Severity.INFO
    data: dict | None = None

    @property
    def emoji(self) -> str:
        return {
            Severity.INFO: "ℹ️",
            Severity.WARNING: "⚠️",
            Severity.CRITICAL: "🚨",
            Severity.SUCCESS: "✅",
        }[self.severity]

    def to_telegram_text(self) -> str:
        """Форматировать для Telegram (HTML)."""
        lines = [f"{self.emoji} <b>{self.title}</b>", ""]
        lines.append(self.message)
        if self.data:
            lines.append("")
            for k, v in self.data.items():
                lines.append(f"  • <b>{k}</b>: <code>{v}</code>")
        lines.append(f"\n🕐 {time.strftime('%H:%M:%S')}")
        return "\n".join(lines)


class TelegramSender:
    """Отправка уведомлений через Telegram Bot API."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        if self.enabled:
            logger.info(f"📱 Telegram: подключён (chat_id: {chat_id})")
        else:
            logger.warning("📱 Telegram: не настроен (установите TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env)")

    def send(self, notification: Notification) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": notification.to_telegram_text(),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except urllib.error.URLError as e:
            logger.error(f"Telegram error: {e}")
            return False


class Notifier:
    """Главный класс системы уведомлений."""

    def __init__(self):
        self.bus = RedisBus()
        self.telegram = TelegramSender(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self._last_task_id = "0"
        self._last_incident_id = "0"
        self._last_decision_id = "0"

    def start(self):
        """Слушать Redis и отправлять уведомления."""
        logger.info("=" * 50)
        logger.info("🔔 NOTIFIER запущен")
        logger.info(f"   Telegram: {'✅' if self.telegram.enabled else '❌ не настроен'}")
        logger.info("=" * 50)

        if not self.bus.ping():
            logger.error("❌ Redis недоступен!")
            return

        try:
            while True:
                self._poll_streams()
                time.sleep(3)
        except KeyboardInterrupt:
            logger.info("Notifier остановлен.")
        finally:
            self.bus.close()

    def _poll_streams(self):
        """Проверить новые записи в Redis Streams."""
        # Инциденты (всегда уведомляем)
        incidents = self.bus.read_log(LogStream.INCIDENTS, count=10)
        for inc in incidents:
            entry_id = inc.get("_id", "0")
            if entry_id > self._last_incident_id:
                self._last_incident_id = entry_id
                self._handle_incident(inc)

        # Задачи (уведомляем о провалах)
        tasks = self.bus.read_log(LogStream.TASKS, count=10)
        for task in tasks:
            entry_id = task.get("_id", "0")
            if entry_id > self._last_task_id:
                self._last_task_id = entry_id
                self._handle_task(task)

    def _handle_incident(self, data: dict):
        """Обработать инцидент."""
        event = data.get("event", "unknown")
        notif = Notification(
            title=f"Инцидент: {event}",
            message=data.get("error", data.get("summary", "Подробности отсутствуют")),
            severity=Severity.CRITICAL,
            data={k: v for k, v in data.items() if k not in ("event", "error", "summary", "_id", "timestamp")},
        )
        self._send(notif)

    def _handle_task(self, data: dict):
        """Обработать результат задачи."""
        event = data.get("event", "")
        task_id = data.get("task_id", "?")

        if event == "task_failed":
            notif = Notification(
                title=f"Задача провалена: {task_id}",
                message=data.get("error", "Неизвестная ошибка"),
                severity=Severity.WARNING,
                data={"role": data.get("role", "?")},
            )
            self._send(notif)
        elif event == "task_completed":
            dur = data.get("duration_sec", 0)
            cost = data.get("cost", 0)
            # Уведомляем только о длинных/дорогих задачах
            if dur > 120 or cost > 50:
                notif = Notification(
                    title=f"Задача выполнена: {task_id}",
                    message=f"Роль: {data.get('role', '?')}",
                    severity=Severity.SUCCESS,
                    data={"время": f"{dur:.0f}с", "стоимость": f"{cost:.1f} U"},
                )
                self._send(notif)

    def _send(self, notification: Notification):
        """Отправить уведомление всеми каналами."""
        log_msg = f"{notification.emoji} {notification.title}: {notification.message}"
        if notification.severity == Severity.CRITICAL:
            logger.critical(log_msg)
        elif notification.severity == Severity.WARNING:
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

        self.telegram.send(notification)


if __name__ == "__main__":
    notifier = Notifier()
    notifier.start()
