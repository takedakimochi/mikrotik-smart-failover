#!/bin/bash

# Dapatkan absolute path direktori script ini
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SERVICE_FILE="ping-monitor.service"

# Pastikan script dijalankan sebagai root atau dengan sudo
if [ "$EUID" -ne 0 ]; then
  echo "❌ Error: Harap jalankan script ini menggunakan sudo!"
  echo "Contoh: sudo ./install.sh"
  exit 1
fi

echo "============================================="
echo "⚙️  Menginstall Systemd Service Ping Monitor..."
echo "============================================="

# Copy file service ke direktori systemd
if [ -f "$SCRIPT_DIR/$SERVICE_FILE" ]; then
  echo "1. Menyalin $SERVICE_FILE ke /etc/systemd/system/..."
  cp "$SCRIPT_DIR/$SERVICE_FILE" /etc/systemd/system/
else
  echo "❌ Error: File $SERVICE_FILE tidak ditemukan di $SCRIPT_DIR!"
  exit 1
fi

# Reload daemon systemd
echo "2. Memuat ulang konfigurasi systemd (daemon-reload)..."
systemctl daemon-reload

# Enable service agar auto start saat boot
echo "3. Mengaktifkan service untuk auto-start saat booting..."
systemctl enable ping-monitor.service

# Jalankan service sekarang
echo "4. Menjalankan service sekarang..."
systemctl start ping-monitor.service

echo "============================================="
echo "✅ Instalasi Selesai!"
echo "============================================="
echo "Gunakan perintah berikut untuk melihat status:"
echo "👉 sudo systemctl status ping-monitor.service"
echo "---------------------------------------------"
echo "Gunakan perintah berikut untuk melihat log aktivitas:"
echo "👉 sudo journalctl -u ping-monitor.service -f"
echo "============================================="
