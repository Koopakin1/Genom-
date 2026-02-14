"""
Memory — Управление долгосрочной памятью (MemGPT-подход).

Интеграция с ChromaDB v2 API для хранения:
- Истории инцидентов (Case Law)
- Профилей проектов
- Технического паспорта сервера
"""

from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("genome.memory")

CHROMA_BASE_URL = "http://localhost:8100"
TENANT = "default_tenant"
DATABASE = "default_database"


@dataclass
class MemoryEntry:
    """Запись в памяти."""
    content: str
    category: str  # incident | project | config | decision | task_result
    metadata: dict | None = None
    timestamp: float | None = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()
        if self.metadata is None:
            self.metadata = {}


class MemoryStore:
    """Хранилище долгосрочной памятью на базе ChromaDB v2 API."""

    def __init__(self, base_url: str = CHROMA_BASE_URL, collection_name: str = "genome_memory"):
        self._base_url = base_url.rstrip("/")
        self._collection_name = collection_name
        self._collection_id: str | None = None

    async def initialize(self) -> None:
        """Создать или получить коллекцию через v2 API."""
        base = f"{self._base_url}/api/v2/tenants/{TENANT}/databases/{DATABASE}"
        async with httpx.AsyncClient(timeout=10) as client:
            # Сначала пробуем получить коллекцию
            resp = await client.get(f"{base}/collections/{self._collection_name}")
            if resp.status_code == 200:
                data = resp.json()
                self._collection_id = data.get("id")
                logger.info(f"Коллекция '{self._collection_name}' найдена: {self._collection_id}")
                return

            # Если не нашли — создаём
            resp = await client.post(
                f"{base}/collections",
                json={"name": self._collection_name},
            )
            if resp.status_code == 200:
                data = resp.json()
                self._collection_id = data.get("id")
                logger.info(f"Коллекция '{self._collection_name}' создана: {self._collection_id}")
            else:
                logger.warning(f"ChromaDB: не удалось создать коллекцию: {resp.status_code} {resp.text}")

    async def store(self, entry: MemoryEntry, entry_id: str | None = None) -> str | None:
        """Сохранить запись в память."""
        if not self._collection_id:
            await self.initialize()
        if not self._collection_id:
            return None

        doc_id = entry_id or f"{entry.category}_{int(entry.timestamp * 1000)}"
        metadata = {
            **(entry.metadata or {}),
            "category": entry.category,
            "timestamp": str(entry.timestamp),
        }

        base = f"{self._base_url}/api/v2/tenants/{TENANT}/databases/{DATABASE}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{base}/collections/{self._collection_id}/add",
                json={
                    "ids": [doc_id],
                    "documents": [entry.content],
                    "metadatas": [metadata],
                },
            )
            if resp.status_code == 200:
                logger.debug(f"Память: сохранена запись {doc_id}")
                return doc_id
            else:
                logger.warning(f"ChromaDB store error: {resp.status_code}")
                return None

    async def search(self, query: str, n_results: int = 5, category: str | None = None) -> list[dict]:
        """Поиск в памяти по смыслу."""
        if not self._collection_id:
            await self.initialize()
        if not self._collection_id:
            return []

        body: dict = {"query_texts": [query], "n_results": n_results}
        if category:
            body["where"] = {"category": category}

        base = f"{self._base_url}/api/v2/tenants/{TENANT}/databases/{DATABASE}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{base}/collections/{self._collection_id}/query",
                json=body,
            )
            if resp.status_code != 200:
                return []

            data = resp.json()
            results = []
            ids = data.get("ids", [[]])[0]
            docs = data.get("documents", [[]])[0]
            metas = data.get("metadatas", [[]])[0]
            distances = data.get("distances", [[]])[0]

            for i, doc_id in enumerate(ids):
                results.append({
                    "id": doc_id,
                    "content": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] if i < len(metas) else {},
                    "distance": distances[i] if i < len(distances) else 0,
                })
            return results

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
            results = []
            ids = data.get("ids", [])
            docs = data.get("documents", [])
            metas = data.get("metadatas", [])

            for i, doc_id in enumerate(ids):
                results.append({
                    "id": doc_id,
                    "content": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] if i < len(metas) else {},
                })
            return results
