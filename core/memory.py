"""
Memory — Управление долгосрочной памятью (MemGPT-подход).

Интеграция с ChromaDB для хранения:
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


@dataclass
class MemoryEntry:
    """Запись в памяти."""
    content: str
    category: str  # incident | project | config | decision
    metadata: dict | None = None
    timestamp: float | None = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()
        if self.metadata is None:
            self.metadata = {}


class MemoryStore:
    """Хранилище долгосрочной памяти на базе ChromaDB."""

    def __init__(self, base_url: str = CHROMA_BASE_URL, collection_name: str = "genome_memory"):
        self._base_url = base_url.rstrip("/")
        self._collection_name = collection_name
        self._collection_id: str | None = None

    async def initialize(self) -> None:
        """Создать или получить коллекцию."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=10) as client:
            resp = await client.post(
                "/api/v1/collections",
                json={"name": self._collection_name, "get_or_create": True},
            )
            if resp.status_code == 200:
                data = resp.json()
                self._collection_id = data.get("id")
                logger.info(f"Коллекция '{self._collection_name}' готова: {self._collection_id}")
            else:
                logger.error(f"Ошибка создания коллекции: {resp.status_code} {resp.text}")

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

        async with httpx.AsyncClient(base_url=self._base_url, timeout=10) as client:
            resp = await client.post(
                f"/api/v1/collections/{self._collection_id}/add",
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
                logger.error(f"Ошибка записи в память: {resp.text}")
                return None

    async def search(self, query: str, n_results: int = 5, category: str | None = None) -> list[dict]:
        """Поиск в памяти по смыслу."""
        if not self._collection_id:
            await self.initialize()
        if not self._collection_id:
            return []

        body: dict = {
            "query_texts": [query],
            "n_results": n_results,
        }
        if category:
            body["where"] = {"category": category}

        async with httpx.AsyncClient(base_url=self._base_url, timeout=10) as client:
            resp = await client.post(
                f"/api/v1/collections/{self._collection_id}/query",
                json=body,
            )
            if resp.status_code != 200:
                logger.error(f"Ошибка поиска: {resp.text}")
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

        async with httpx.AsyncClient(base_url=self._base_url, timeout=10) as client:
            resp = await client.post(
                f"/api/v1/collections/{self._collection_id}/get",
                json={
                    "where": {"category": category},
                    "limit": limit,
                },
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
