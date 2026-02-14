"""
Memory — Иерархическое управление памятью (MemGPT-подход).

Три уровня памяти (вдохновлено MemGPT: https://research.memgpt.ai/):

1. Core Memory (оперативная) — текущий контекст, всегда в промпте.
   Хранится в Redis, мгновенный доступ.

2. Recall Memory (буферная) — недавние события и результаты.
   Хранится в Redis Streams, FIFO с ограничением.

3. Archival Memory (долгосрочная) — семантический поиск.
   Хранится в ChromaDB, поиск по смыслу.

Принцип "виртуального контекста": LLM работает с ограниченным окном,
а система автоматически подгружает релевантные данные из глубоких слоёв
(аналог paging в ОС: RAM ↔ Disk).
"""

from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass, field

import httpx
import redis

logger = logging.getLogger("genome.memory")

CHROMA_BASE_URL = "http://localhost:8100"
OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "qwen2.5:1.5b"
TENANT = "default_tenant"
DATABASE = "default_database"
REDIS_HOST = "localhost"
REDIS_PORT = 6379


async def _embed(texts: list[str]) -> list[list[float]] | None:
    """Генерация embeddings через Ollama /api/embed."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": EMBED_MODEL, "input": texts},
            )
            if resp.status_code == 200:
                return resp.json().get("embeddings")
    except Exception as e:
        logger.warning(f"Embedding error: {e}")
    return None


# ============================================================
# Модели данных
# ============================================================

@dataclass
class MemoryEntry:
    """Запись в памяти."""
    content: str
    category: str  # incident | project | config | decision | task_result | persona
    metadata: dict | None = None
    timestamp: float | None = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()
        if self.metadata is None:
            self.metadata = {}


# ============================================================
# Уровень 1: Core Memory (оперативная — Redis)
# ============================================================

class CoreMemory:
    """Оперативная память — текущий контекст системы.

    Всегда включается в промпт. Содержит:
    - persona: кто я (роль, возможности)
    - system_state: текущее состояние системы
    - human_prefs: предпочтения оператора
    """

    REDIS_KEY = "MEMORY:CORE"

    def __init__(self, host: str = REDIS_HOST, port: int = REDIS_PORT):
        self._r = redis.Redis(host=host, port=port, decode_responses=True)
        self._defaults = {
            "persona": (
                "Я — Администрация ИИ-Полиса ГЕНОМ. "
                "Управляю задачами через ЖКХ-роли (sysadmin, auditor, economist, cleaner, mchs). "
                "Оцениваю стоимость в Юнитах. Приоритет: безопасность > производительность > экономия."
            ),
            "system_state": json.dumps({
                "models": {"admin": "qwen2.5:1.5b", "worker": "llama3.1:8b"},
                "gpu": "AMD RX 560",
                "status": "operational",
            }, ensure_ascii=False),
            "human_prefs": json.dumps({
                "language": "ru",
                "verbose_logs": True,
                "auto_cleanup": True,
            }, ensure_ascii=False),
        }

    def initialize(self):
        """Инициализировать core memory значениями по умолчанию (если пуста)."""
        for key, default in self._defaults.items():
            if not self._r.hexists(self.REDIS_KEY, key):
                self._r.hset(self.REDIS_KEY, key, default)
        logger.info("Core Memory: инициализирована")

    def get(self, key: str) -> str:
        """Прочитать блок core memory."""
        return self._r.hget(self.REDIS_KEY, key) or self._defaults.get(key, "")

    def set(self, key: str, value: str):
        """Обновить блок core memory (LLM может это делать сам)."""
        self._r.hset(self.REDIS_KEY, key, value)
        logger.debug(f"Core Memory updated: {key}")

    def get_all(self) -> dict:
        """Получить весь core memory для включения в промпт."""
        return self._r.hgetall(self.REDIS_KEY) or dict(self._defaults)

    def to_prompt_block(self) -> str:
        """Сформировать блок для промпта."""
        data = self.get_all()
        lines = ["<core_memory>"]
        for key, value in data.items():
            lines.append(f"[{key}]")
            lines.append(value)
            lines.append("")
        lines.append("</core_memory>")
        return "\n".join(lines)


# ============================================================
# Уровень 2: Recall Memory (буферная — Redis Streams)
# ============================================================

class RecallMemory:
    """Буферная память — недавние события (FIFO).

    Хранит последние N событий в Redis Stream.
    Автоматически усекается при превышении лимита.
    """

    STREAM_KEY = "MEMORY:RECALL"
    MAX_ENTRIES = 100

    def __init__(self, host: str = REDIS_HOST, port: int = REDIS_PORT):
        self._r = redis.Redis(host=host, port=port, decode_responses=True)

    def append(self, event_type: str, content: str, metadata: dict | None = None):
        """Добавить событие в recall memory."""
        entry = {
            "event": event_type,
            "content": content[:500],  # Ограничиваем размер
            "timestamp": str(time.time()),
        }
        if metadata:
            entry["meta"] = json.dumps(metadata, ensure_ascii=False)[:300]

        self._r.xadd(self.STREAM_KEY, entry, maxlen=self.MAX_ENTRIES)

    def get_recent(self, count: int = 10) -> list[dict]:
        """Получить последние N событий."""
        entries = self._r.xrevrange(self.STREAM_KEY, count=count)
        results = []
        for entry_id, data in entries:
            data["_id"] = entry_id
            if "meta" in data:
                try:
                    data["meta"] = json.loads(data["meta"])
                except json.JSONDecodeError:
                    pass
            results.append(data)
        return list(reversed(results))  # Хронологический порядок

    def search(self, query: str, count: int = 20) -> list[dict]:
        """Простой текстовый поиск по recall memory."""
        all_entries = self.get_recent(count=100)
        query_lower = query.lower()
        return [e for e in all_entries if query_lower in e.get("content", "").lower()][:count]

    def to_prompt_block(self, count: int = 5) -> str:
        """Сформировать блок последних событий для промпта."""
        recent = self.get_recent(count)
        if not recent:
            return "<recall_memory>\nНет недавних событий.\n</recall_memory>"
        lines = ["<recall_memory>"]
        for e in recent:
            ts = time.strftime("%H:%M:%S", time.localtime(float(e.get("timestamp", 0))))
            lines.append(f"[{ts}] {e.get('event', '?')}: {e.get('content', '')[:200]}")
        lines.append("</recall_memory>")
        return "\n".join(lines)


# ============================================================
# Уровень 3: Archival Memory (долгосрочная — ChromaDB)
# ============================================================

class ArchivalMemory:
    """Долгосрочная память — семантический поиск.

    Хранит документы в ChromaDB, поиск по embedding-расстоянию.
    Используется для: кейсов, решений, инцидентов, проектов.
    """

    def __init__(self, base_url: str = CHROMA_BASE_URL, collection_name: str = "genome_memory"):
        self._base_url = base_url.rstrip("/")
        self._collection_name = collection_name
        self._collection_id: str | None = None

    async def initialize(self) -> None:
        """Создать/получить коллекцию через v2 API."""
        base = f"{self._base_url}/api/v2/tenants/{TENANT}/databases/{DATABASE}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base}/collections/{self._collection_name}")
            if resp.status_code == 200:
                self._collection_id = resp.json().get("id")
                logger.info(f"Archival Memory: коллекция найдена ({self._collection_id})")
                return

            resp = await client.post(f"{base}/collections", json={"name": self._collection_name})
            if resp.status_code == 200:
                self._collection_id = resp.json().get("id")
                logger.info(f"Archival Memory: коллекция создана ({self._collection_id})")
            else:
                logger.warning(f"ChromaDB: {resp.status_code}")

    async def insert(self, entry: MemoryEntry, entry_id: str | None = None) -> str | None:
        """Сохранить запись в архивную память (с Ollama embeddings)."""
        if not self._collection_id:
            await self.initialize()
        if not self._collection_id:
            return None

        # Генерируем embedding через Ollama
        embeddings = await _embed([entry.content])
        if not embeddings:
            logger.warning("Archival: не удалось сгенерировать embedding")
            return None

        doc_id = entry_id or f"{entry.category}_{int((entry.timestamp or time.time()) * 1000)}"
        metadata = {**(entry.metadata or {}), "category": entry.category, "timestamp": str(entry.timestamp)}

        base = f"{self._base_url}/api/v2/tenants/{TENANT}/databases/{DATABASE}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{base}/collections/{self._collection_id}/add",
                json={
                    "ids": [doc_id],
                    "embeddings": embeddings,
                    "documents": [entry.content],
                    "metadatas": [metadata],
                },
            )
            if resp.status_code in (200, 201):
                logger.debug(f"Archival: сохранено {doc_id}")
                return doc_id
        return None

    async def search(self, query: str, n_results: int = 5, category: str | None = None) -> list[dict]:
        """Семантический поиск через Ollama embeddings."""
        if not self._collection_id:
            await self.initialize()
        if not self._collection_id:
            return []

        # Генерируем embedding запроса через Ollama
        query_emb = await _embed([query])
        if not query_emb:
            return []

        body: dict = {"query_embeddings": query_emb, "n_results": n_results}
        if category:
            body["where"] = {"category": category}

        base = f"{self._base_url}/api/v2/tenants/{TENANT}/databases/{DATABASE}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{base}/collections/{self._collection_id}/query", json=body)
            if resp.status_code != 200:
                logger.warning(f"Archival search: {resp.status_code} {resp.text[:200]}")
                return []
            data = resp.json()
            ids = data.get("ids", [[]])[0]
            docs = data.get("documents", [[]])[0]
            metas = data.get("metadatas", [[]])[0]
            dists = data.get("distances", [[]])[0]
            return [
                {"id": ids[i], "content": docs[i], "metadata": metas[i], "distance": dists[i]}
                for i in range(len(ids))
            ]

    async def get_recent(self, category: str, limit: int = 10) -> list[dict]:
        """Получить последние записи категории."""
        if not self._collection_id:
            await self.initialize()
        if not self._collection_id:
            return []

        base = f"{self._base_url}/api/v2/tenants/{TENANT}/databases/{DATABASE}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{base}/collections/{self._collection_id}/get",
                json={"where": {"category": category}, "limit": limit},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            ids = data.get("ids", [])
            docs = data.get("documents", [])
            metas = data.get("metadatas", [])
            return [{"id": ids[i], "content": docs[i], "metadata": metas[i]} for i in range(len(ids))]


# ============================================================
# MemoryStore — Единый интерфейс (обратная совместимость)
# ============================================================

class MemoryStore:
    """Единый интерфейс к 3 уровням памяти (MemGPT-подход).

    Virtual context = Core (always in prompt) + Recall (recent events)
                    + Archival (semantic search on demand)
    """

    def __init__(self, base_url: str = CHROMA_BASE_URL, collection_name: str = "genome_memory"):
        self.core = CoreMemory()
        self.recall = RecallMemory()
        self.archival = ArchivalMemory(base_url, collection_name)

    async def initialize(self) -> None:
        """Инициализация всех уровней."""
        self.core.initialize()
        await self.archival.initialize()
        logger.info("MemoryStore: 3 уровня памяти готовы (Core/Recall/Archival)")

    async def store(self, entry: MemoryEntry, entry_id: str | None = None) -> str | None:
        """Сохранить в recall + archival (обратная совместимость)."""
        self.recall.append(entry.category, entry.content, entry.metadata)
        return await self.archival.insert(entry, entry_id)

    async def search(self, query: str, n_results: int = 5, category: str | None = None) -> list[dict]:
        """Семантический поиск (archival)."""
        return await self.archival.search(query, n_results, category)

    def build_context(self, max_recall: int = 5) -> str:
        """Построить виртуальный контекст для промпта (MemGPT-стиль).

        Включает Core Memory (persona, state) + Recall Memory (последние события).
        Archival memory подгружается по запросу через search().
        """
        parts = [
            self.core.to_prompt_block(),
            self.recall.to_prompt_block(count=max_recall),
        ]
        return "\n\n".join(parts)
