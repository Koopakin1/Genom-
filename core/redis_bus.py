"""
Redis Bus — Шина данных ИИ-Полиса «ГЕНОМ».

Обёртка над Redis для работы с очередями, pub/sub каналами,
состоянием и логами системы.
"""

from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

import redis

logger = logging.getLogger("genome.redis_bus")


class QueuePriority(str, Enum):
    CRITICAL = "QUEUE:CRITICAL"
    EXPORT = "QUEUE:EXPORT"
    INTERNAL = "QUEUE:INTERNAL"


class Channel(str, Enum):
    SIGNALS = "CHANNEL:SIGNALS"
    HEARTBEAT = "CHANNEL:HEARTBEAT"


class StateKey(str, Enum):
    WORKER_CURRENT = "STATE:WORKER:CURRENT"
    WORKER_STATUS = "STATE:WORKER:STATUS"
    BUDGET_AVAILABLE = "STATE:BUDGET:AVAILABLE"
    BUDGET_RESERVED = "STATE:BUDGET:RESERVED"


class LogStream(str, Enum):
    DECISIONS = "LOG:DECISIONS"
    TASKS = "LOG:TASKS"
    INCIDENTS = "LOG:INCIDENTS"


@dataclass
class Task:
    """Задача в очереди."""
    task_id: str
    task_type: str
    payload: dict = field(default_factory=dict)
    priority: str = "export"
    source: str = "unknown"
    created_at: float = field(default_factory=time.time)
    estimated_units: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str | bytes) -> Task:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        d = json.loads(data)
        return cls(**d)


class RedisBus:
    """Шина данных на базе Redis."""

    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0):
        self._client = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        self._pubsub = self._client.pubsub()
        logger.info(f"RedisBus подключён к {host}:{port}")

    def ping(self) -> bool:
        """Проверка соединения."""
        try:
            return self._client.ping()
        except redis.ConnectionError:
            return False

    # ========================
    # Очереди задач
    # ========================

    def push_task(self, task: Task, priority: QueuePriority | None = None) -> None:
        """Поставить задачу в очередь."""
        if priority is None:
            priority_map = {
                "critical": QueuePriority.CRITICAL,
                "export": QueuePriority.EXPORT,
                "internal": QueuePriority.INTERNAL,
            }
            priority = priority_map.get(task.priority, QueuePriority.INTERNAL)
        self._client.lpush(priority.value, task.to_json())
        logger.debug(f"Задача {task.task_id} → {priority.value}")

    def pop_task(self, timeout: int = 5) -> Task | None:
        """
        Получить задачу из очередей с приоритетом:
        CRITICAL → EXPORT → INTERNAL.
        Блокирующий вызов с таймаутом.
        """
        result = self._client.brpop(
            [q.value for q in QueuePriority],
            timeout=timeout,
        )
        if result is None:
            return None
        _queue, data = result
        task = Task.from_json(data)
        logger.debug(f"Задача {task.task_id} ← {_queue}")
        return task

    def queue_length(self, priority: QueuePriority) -> int:
        """Размер очереди."""
        return self._client.llen(priority.value)

    def queue_lengths(self) -> dict[str, int]:
        """Размеры всех очередей."""
        return {q.name: self.queue_length(q) for q in QueuePriority}

    # ========================
    # Pub/Sub каналы
    # ========================

    def publish(self, channel: Channel, message: dict[str, Any]) -> None:
        """Послать сигнал."""
        self._client.publish(channel.value, json.dumps(message, ensure_ascii=False))

    def subscribe(self, channel: Channel) -> None:
        """Подписаться на канал."""
        self._pubsub.subscribe(channel.value)

    def listen(self):
        """Генератор сообщений из подписки."""
        for message in self._pubsub.listen():
            if message["type"] == "message":
                yield json.loads(message["data"])

    # ========================
    # Состояние системы
    # ========================

    def set_state(self, key: StateKey, value: str) -> None:
        self._client.set(key.value, value)

    def get_state(self, key: StateKey) -> str | None:
        return self._client.get(key.value)

    def set_budget(self, key: StateKey, data: dict[str, float]) -> None:
        self._client.hset(key.value, mapping={k: str(v) for k, v in data.items()})

    def get_budget(self, key: StateKey) -> dict[str, float]:
        raw = self._client.hgetall(key.value)
        return {k: float(v) for k, v in raw.items()}

    # ========================
    # Логирование (Redis Streams)
    # ========================

    def log(self, stream: LogStream, data: dict[str, Any]) -> str:
        """Записать в лог-стрим. Возвращает ID записи."""
        data["timestamp"] = time.time()
        entry = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in data.items()}
        entry_id = self._client.xadd(stream.value, entry, maxlen=10000)
        return entry_id

    def read_log(self, stream: LogStream, count: int = 10, last_id: str = "0") -> list[dict]:
        """Прочитать последние записи из лог-стрима."""
        entries = self._client.xrange(stream.value, min=last_id, count=count)
        results = []
        for entry_id, data in entries:
            parsed = {}
            for k, v in data.items():
                try:
                    parsed[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    parsed[k] = v
            parsed["_id"] = entry_id
            results.append(parsed)
        return results

    def close(self) -> None:
        self._pubsub.close()
        self._client.close()
