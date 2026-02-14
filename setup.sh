#!/usr/bin/env bash
# =============================================
# ГЕНОМ — Скрипт первоначальной настройки
# =============================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=============================================="
echo "🏙️  ИИ-ПОЛИС «ГЕНОМ» — ПЕРВОНАЧАЛЬНАЯ НАСТРОЙКА"
echo "=============================================="
echo ""

# 1. Python-зависимости
echo "📦 [1/4] Установка Python-зависимостей..."
pip3 install --break-system-packages --user -q -r requirements.txt
echo "   ✅ Python-пакеты установлены"
echo ""

# 2. Docker-инфраструктура
echo "🐋 [2/4] Запуск Docker-инфраструктуры..."
docker compose up -d
echo "   ✅ Redis, ChromaDB, Ollama запущены"
echo ""

# 3. Ожидание готовности Ollama
echo "⏳ [3/4] Ожидание готовности Ollama..."
MAX_WAIT=60
WAITED=0
until curl -sf http://localhost:11434/ > /dev/null 2>&1; do
    sleep 2
    WAITED=$((WAITED + 2))
    if [ $WAITED -ge $MAX_WAIT ]; then
        echo "   ❌ Ollama не запустился за ${MAX_WAIT}с"
        exit 1
    fi
    echo "   ⏳ Ждём... (${WAITED}с)"
done
echo "   ✅ Ollama готов"
echo ""

# 4. Загрузка и регистрация моделей
echo "🧠 [4/4] Загрузка моделей (это займёт время)..."

echo "   📥 Загрузка qwen2.5:1.5b (Администрация)..."
docker exec genome-ollama ollama pull qwen2.5:1.5b

echo "   📥 Загрузка llama3.2:3b (ЖКХ)..."
docker exec genome-ollama ollama pull llama3.2:3b

echo ""
echo "   📝 Регистрация Modelfile-ролей..."

# Копируем Modelfiles в контейнер и создаём модели
for role in admin sysadmin auditor economist cleaner mchs; do
    MODELFILE="modelfiles/Modelfile.${role}"
    if [ -f "$MODELFILE" ]; then
        # Для admin используем уменьшенную модель (1.5B)
        if [ "$role" = "admin" ]; then
            MODEL_NAME="genome-admin"
        else
            MODEL_NAME="genome-worker-${role}"
            # Заменяем 8b на 3b для экономии RAM (CPU-only)
            sed -i 's/llama3.2:8b-instruct-q4_K_M/llama3.2:3b/g' "$MODELFILE" 2>/dev/null || true
        fi

        # Копируем Modelfile в контейнер
        docker cp "$MODELFILE" genome-ollama:/tmp/Modelfile
        docker exec genome-ollama ollama create "$MODEL_NAME" -f /tmp/Modelfile
        echo "   ✅ ${MODEL_NAME} зарегистрирована"
    fi
done

echo ""
echo "=============================================="
echo "✅ НАСТРОЙКА ЗАВЕРШЕНА!"
echo "=============================================="
echo ""
echo "Доступные команды:"
echo "  • Запуск Администрации:  cd $(pwd) && python3 -m core.orchestrator"
echo "  • Запуск Watchdog:       cd $(pwd) && python3 -m security.watchdog"
echo "  • Проверка Redis:        redis-cli -h localhost ping"
echo "  • Проверка Ollama:       curl http://localhost:11434/api/tags"
echo "  • Список моделей:        docker exec genome-ollama ollama list"
echo ""
echo "Для отправки тестовой задачи:"
echo "  python3 -c \""
echo "    import redis, json, time"
echo "    r = redis.Redis()"
echo "    task = {'task_id': 'test_001', 'task_type': 'diagnostics', 'payload': {'message': 'Тестовая задача'}, 'priority': 'export', 'source': 'manual', 'created_at': time.time(), 'estimated_units': 0}"
echo "    r.lpush('QUEUE:EXPORT', json.dumps(task))"
echo "    print('Задача отправлена!')"
echo "  \""
