#!/bin/bash
# install_services.sh — Установка systemd-сервисов ГЕНОМ
set -e

SERVICES_DIR="$(dirname "$0")/systemd"

echo "📦 Установка systemd-сервисов ГЕНОМ..."
echo ""

for service in genome-orchestrator genome-dashboard genome-watchdog; do
    SRC="${SERVICES_DIR}/${service}.service"
    DEST="/etc/systemd/system/${service}.service"
    
    if [ ! -f "$SRC" ]; then
        echo "  ❌ ${service}.service не найден"
        continue
    fi
    
    sudo cp "$SRC" "$DEST"
    echo "  ✅ ${service}.service → ${DEST}"
done

echo ""
echo "🔄 Перезагрузка systemd..."
sudo systemctl daemon-reload

echo ""
echo "🚀 Включение и запуск..."
sudo systemctl enable genome-orchestrator genome-dashboard genome-watchdog
sudo systemctl start genome-orchestrator genome-dashboard genome-watchdog

echo ""
echo "📊 Статус:"
sudo systemctl status genome-orchestrator genome-dashboard genome-watchdog --no-pager -l | head -30

echo ""
echo "✅ Готово! Сервисы будут автоматически стартовать при загрузке."
echo ""
echo "Полезные команды:"
echo "  sudo systemctl status genome-orchestrator    # Статус"
echo "  sudo journalctl -u genome-orchestrator -f     # Логи"
echo "  sudo systemctl restart genome-orchestrator    # Перезапуск"
