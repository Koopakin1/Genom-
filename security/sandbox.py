"""
Sandbox — Docker-песочница для выполнения кода НИИ.

Изолированная среда для безопасного запуска кода,
сгенерированного нейросетями.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("genome.sandbox")

# Лимиты для песочницы
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_MEMORY_LIMIT = "256m"
DEFAULT_CPU_QUOTA = 50000  # 50% одного ядра
NETWORK_DISABLED = True


@dataclass
class SandboxResult:
    """Результат выполнения кода в песочнице."""
    sandbox_id: str
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float
    killed: bool = False     # Убит по таймауту
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "sandbox_id": self.sandbox_id,
            "success": self.success,
            "exit_code": self.exit_code,
            "stdout": self.stdout[:2000],  # Обрезка для безопасности
            "stderr": self.stderr[:1000],
            "duration_sec": round(self.duration_sec, 2),
            "killed": self.killed,
            "error": self.error,
        }


class DockerSandbox:
    """Docker-песочница для безопасного выполнения кода."""

    def __init__(
        self,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        cpu_quota: int = DEFAULT_CPU_QUOTA,
        network_disabled: bool = NETWORK_DISABLED,
    ):
        self.timeout_sec = timeout_sec
        self.memory_limit = memory_limit
        self.cpu_quota = cpu_quota
        self.network_disabled = network_disabled

    async def execute_python(self, code: str, stdin_data: str = "") -> SandboxResult:
        """Выполнить Python-код в изолированном контейнере."""
        sandbox_id = f"sandbox_{uuid.uuid4().hex[:8]}"

        # Создаём временный файл с кодом
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="genome_", delete=False
        ) as f:
            f.write(code)
            code_path = f.name

        try:
            cmd = [
                "docker", "run",
                "--rm",
                "--name", sandbox_id,
                f"--memory={self.memory_limit}",
                f"--cpu-quota={self.cpu_quota}",
                "--pids-limit=50",
                "--read-only",
                "--tmpfs=/tmp:size=10m",
                "--security-opt=no-new-privileges",
            ]

            if self.network_disabled:
                cmd.append("--network=none")

            # Монтируем код только для чтения
            cmd.extend([
                "-v", f"{code_path}:/app/script.py:ro",
                "python:3.12-slim",
                "python", "/app/script.py",
            ])

            import time
            start = time.time()

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if stdin_data else None,
            )

            killed = False
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=stdin_data.encode() if stdin_data else None),
                    timeout=self.timeout_sec,
                )
            except asyncio.TimeoutError:
                killed = True
                # Убиваем контейнер
                kill_proc = await asyncio.create_subprocess_exec(
                    "docker", "kill", sandbox_id,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await kill_proc.wait()
                stdout, stderr = b"", b"Execution timed out"

            duration = time.time() - start

            return SandboxResult(
                sandbox_id=sandbox_id,
                success=proc.returncode == 0 and not killed,
                exit_code=proc.returncode or -1,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                duration_sec=duration,
                killed=killed,
            )

        except Exception as e:
            return SandboxResult(
                sandbox_id=sandbox_id,
                success=False,
                exit_code=-1,
                stdout="",
                stderr="",
                duration_sec=0,
                error=str(e),
            )
        finally:
            # Удаляем временный файл
            Path(code_path).unlink(missing_ok=True)

    async def execute_bash(self, script: str) -> SandboxResult:
        """Выполнить bash-скрипт в песочнице."""
        sandbox_id = f"sandbox_{uuid.uuid4().hex[:8]}"

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", prefix="genome_", delete=False
        ) as f:
            f.write(script)
            script_path = f.name

        try:
            cmd = [
                "docker", "run",
                "--rm",
                "--name", sandbox_id,
                f"--memory={self.memory_limit}",
                f"--cpu-quota={self.cpu_quota}",
                "--pids-limit=50",
                "--read-only",
                "--tmpfs=/tmp:size=10m",
                "--security-opt=no-new-privileges",
                "--network=none",
                "-v", f"{script_path}:/app/script.sh:ro",
                "alpine:latest",
                "sh", "/app/script.sh",
            ]

            import time
            start = time.time()

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            killed = False
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.timeout_sec,
                )
            except asyncio.TimeoutError:
                killed = True
                kill_proc = await asyncio.create_subprocess_exec(
                    "docker", "kill", sandbox_id,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await kill_proc.wait()
                stdout, stderr = b"", b"Execution timed out"

            duration = time.time() - start

            return SandboxResult(
                sandbox_id=sandbox_id,
                success=proc.returncode == 0 and not killed,
                exit_code=proc.returncode or -1,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                duration_sec=duration,
                killed=killed,
            )

        except Exception as e:
            return SandboxResult(
                sandbox_id=sandbox_id,
                success=False,
                exit_code=-1,
                stdout="",
                stderr="",
                duration_sec=0,
                error=str(e),
            )
        finally:
            Path(script_path).unlink(missing_ok=True)
