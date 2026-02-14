"""
Static Analysis — Ревизор: статический анализ кода.

Проверяет код, сгенерированный нейросетями, на наличие
опасных паттернов ПЕРЕД выполнением в песочнице.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("genome.static_analysis")


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingType(str, Enum):
    DESTRUCTIVE = "destructive"        # Деструктивные команды
    NETWORK = "network"                # Сетевые вызовы
    PRIVILEGE = "privilege_escalation"  # Повышение привилегий
    OBFUSCATION = "obfuscation"        # Обфускация кода
    RESOURCE = "resource_abuse"        # Злоупотребление ресурсами
    DATA_LEAK = "data_leak"            # Утечка данных
    INJECTION = "injection"            # Инъекции


@dataclass
class Finding:
    """Находка статического анализа."""
    finding_type: str
    severity: str
    description: str
    line_number: int | None = None
    code_snippet: str = ""
    recommendation: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.finding_type,
            "severity": self.severity,
            "description": self.description,
            "line": self.line_number,
            "snippet": self.code_snippet[:200],
            "recommendation": self.recommendation,
        }


@dataclass
class AnalysisReport:
    """Отчёт статического анализа."""
    safe: bool
    risk_level: int   # 0-10
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "safe": self.safe,
            "risk_level": self.risk_level,
            "findings_count": len(self.findings),
            "findings": [f.to_dict() for f in self.findings],
            "summary": self.summary,
        }


# ==========================================
# Паттерны опасности
# ==========================================

DANGEROUS_PATTERNS: list[tuple[str, str, str, str]] = [
    # (regex, finding_type, severity, description)

    # Деструктивные команды
    (r"rm\s+(-rf?|--recursive)\s+/", FindingType.DESTRUCTIVE, Severity.CRITICAL,
     "Удаление корневой файловой системы"),
    (r"rm\s+(-rf?|--recursive)\s+~", FindingType.DESTRUCTIVE, Severity.HIGH,
     "Удаление домашней директории"),
    (r"mkfs\.", FindingType.DESTRUCTIVE, Severity.CRITICAL,
     "Форматирование файловой системы"),
    (r"dd\s+if=.*of=/dev/", FindingType.DESTRUCTIVE, Severity.CRITICAL,
     "Запись на блочное устройство"),
    (r":\(\)\{\s*:\|:\s*&\s*\};:", FindingType.RESOURCE, Severity.CRITICAL,
     "Fork-бомба"),

    # Сетевые угрозы
    (r"curl\s+.*\|\s*(bash|sh|python)", FindingType.NETWORK, Severity.CRITICAL,
     "Скачивание и выполнение удалённого скрипта"),
    (r"wget\s+.*&&.*\s*(bash|sh|chmod)", FindingType.NETWORK, Severity.CRITICAL,
     "Скачивание и выполнение удалённого файла"),
    (r"(nc|ncat|netcat)\s+(-e|-c|--exec)", FindingType.NETWORK, Severity.CRITICAL,
     "Reverse shell через netcat"),
    (r"socket\.connect\(", FindingType.NETWORK, Severity.MEDIUM,
     "Попытка сетевого подключения"),
    (r"requests\.(get|post|put|delete)\(", FindingType.NETWORK, Severity.LOW,
     "HTTP-запрос через requests"),
    (r"urllib\.request", FindingType.NETWORK, Severity.LOW,
     "HTTP-запрос через urllib"),

    # Повышение привилегий
    (r"sudo\s+", FindingType.PRIVILEGE, Severity.HIGH,
     "Попытка выполнения с повышенными привилегиями"),
    (r"chmod\s+777", FindingType.PRIVILEGE, Severity.HIGH,
     "Открытие полного доступа к файлу"),
    (r"chown\s+root", FindingType.PRIVILEGE, Severity.HIGH,
     "Смена владельца на root"),
    (r"/etc/shadow", FindingType.DATA_LEAK, Severity.CRITICAL,
     "Доступ к файлу паролей"),
    (r"/etc/passwd", FindingType.DATA_LEAK, Severity.MEDIUM,
     "Доступ к файлу пользователей"),

    # Обфускация
    (r"eval\(.*compile\(", FindingType.OBFUSCATION, Severity.HIGH,
     "Динамическая компиляция и выполнение кода"),
    (r"exec\(.*base64", FindingType.OBFUSCATION, Severity.CRITICAL,
     "Выполнение base64-закодированного кода"),
    (r"__import__\(", FindingType.OBFUSCATION, Severity.MEDIUM,
     "Динамический импорт модулей"),
    (r"\\x[0-9a-f]{2}\\x[0-9a-f]{2}\\x[0-9a-f]{2}", FindingType.OBFUSCATION, Severity.MEDIUM,
     "Шестнадцатеричное кодирование строк"),

    # Злоупотребление ресурсами
    (r"while\s+(True|1)\s*:", FindingType.RESOURCE, Severity.LOW,
     "Бесконечный цикл (может быть намеренным)"),
    (r"os\.fork\(\)", FindingType.RESOURCE, Severity.HIGH,
     "Форк процесса"),
    (r"multiprocessing\.Pool\(\d{3,}", FindingType.RESOURCE, Severity.MEDIUM,
     "Создание большого пула процессов"),

    # Утечка данных
    (r"(API_KEY|SECRET|PASSWORD|TOKEN)\s*=\s*['\"]", FindingType.DATA_LEAK, Severity.HIGH,
     "Хардкодинг секретов"),
    (r"\.env", FindingType.DATA_LEAK, Severity.LOW,
     "Доступ к файлу переменных окружения"),

    # Инъекции
    (r"os\.system\(", FindingType.INJECTION, Severity.MEDIUM,
     "Выполнение системных команд через os.system"),
    (r"subprocess\.(call|run|Popen)\(.*shell\s*=\s*True", FindingType.INJECTION, Severity.HIGH,
     "Subprocess с shell=True (уязвим к инъекциям)"),
]


def analyze_code(code: str) -> AnalysisReport:
    """
    Статический анализ кода на опасные паттерны.

    Returns:
        AnalysisReport с результатами анализа.
    """
    findings: list[Finding] = []
    lines = code.split("\n")

    for pattern, finding_type, severity, description in DANGEROUS_PATTERNS:
        try:
            regex = re.compile(pattern, re.IGNORECASE)

            # Проверяем построчно для указания номера строки
            for i, line in enumerate(lines, 1):
                # Пропускаем комментарии
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue

                if regex.search(line):
                    findings.append(Finding(
                        finding_type=finding_type,
                        severity=severity,
                        description=description,
                        line_number=i,
                        code_snippet=line.strip(),
                        recommendation=_get_recommendation(finding_type),
                    ))
        except re.error:
            continue

    # Рассчитываем risk_level
    risk_level = _calculate_risk(findings)
    safe = risk_level <= 3 and not any(
        f.severity == Severity.CRITICAL for f in findings
    )

    # Сводка
    if not findings:
        summary = "Код безопасен. Опасных паттернов не обнаружено."
    elif safe:
        summary = f"Обнаружено {len(findings)} предупреждений низкого риска."
    else:
        critical = sum(1 for f in findings if f.severity == Severity.CRITICAL)
        high = sum(1 for f in findings if f.severity == Severity.HIGH)
        summary = f"⚠️ ОПАСНО: {critical} критических, {high} высоких угроз. Код ЗАБЛОКИРОВАН."

    return AnalysisReport(
        safe=safe,
        risk_level=risk_level,
        findings=findings,
        summary=summary,
    )


def _calculate_risk(findings: list[Finding]) -> int:
    """Рассчитать уровень риска 0-10."""
    if not findings:
        return 0

    severity_weights = {
        Severity.LOW: 1,
        Severity.MEDIUM: 2,
        Severity.HIGH: 4,
        Severity.CRITICAL: 8,
    }

    total = sum(severity_weights.get(Severity(f.severity), 1) for f in findings)
    return min(10, total)


def _get_recommendation(finding_type: str) -> str:
    """Получить рекомендацию по типу угрозы."""
    recommendations = {
        FindingType.DESTRUCTIVE: "Удалите деструктивные команды или замените на безопасные аналоги.",
        FindingType.NETWORK: "Удалите сетевые вызовы или используйте sandbox с отключенной сетью.",
        FindingType.PRIVILEGE: "Удалите команды повышения привилегий.",
        FindingType.OBFUSCATION: "Замените обфусцированный код на читаемый эквивалент.",
        FindingType.RESOURCE: "Добавьте ограничения по ресурсам (таймауты, лимиты).",
        FindingType.DATA_LEAK: "Используйте переменные окружения вместо хардкодинга секретов.",
        FindingType.INJECTION: "Используйте subprocess.run() без shell=True и с массивом аргументов.",
    }
    return recommendations.get(finding_type, "Проверьте код вручную.")
