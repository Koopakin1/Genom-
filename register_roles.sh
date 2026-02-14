#!/bin/bash
# register_roles.sh — Регистрация Modelfile-ролей в Ollama

set -e

echo "📋 Регистрация ролей ЖКХ в Ollama..."
echo ""

MODELFILES_DIR="$(dirname "$0")/modelfiles"
CONTAINER="genome-ollama"

roles=("admin" "sysadmin" "auditor" "economist" "cleaner" "mchs")

for role in "${roles[@]}"; do
    MODELFILE="${MODELFILES_DIR}/Modelfile.${role}"
    if [ ! -f "$MODELFILE" ]; then
        echo "  ❌ Modelfile.${role} не найден"
        continue
    fi

    if [ "$role" = "admin" ]; then
        MODEL_NAME="genome-admin"
    else
        MODEL_NAME="genome-worker-${role}"
    fi

    echo "  🔧 ${MODEL_NAME}..."
    docker cp "$MODELFILE" "${CONTAINER}:/tmp/Modelfile.${role}"
    docker exec "$CONTAINER" ollama create "$MODEL_NAME" -f "/tmp/Modelfile.${role}" 2>&1 | tail -1
done

echo ""
echo "📦 Модели в Ollama:"
docker exec "$CONTAINER" ollama list
echo ""
echo "✅ Регистрация завершена!"
