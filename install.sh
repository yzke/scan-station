#!/usr/bin/env bash
# Install Scan Station on an apt/systemd Linux host.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${SCAN_STATION_ENV_FILE:-/etc/scan-station.env}"
PORT="${PORT:-8081}"
LAN_CIDR="${LAN_CIDR:-}"

python3 - "$SRC_DIR" "$PORT" "$LAN_CIDR" "$ENV_FILE" <<'PY'
import ipaddress
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from configuration import configured_port
configured_port({'SCAN_STATION_PORT': sys.argv[2]})
if sys.argv[3]:
    try:
        ipaddress.ip_network(sys.argv[3])
    except ValueError:
        raise SystemExit('LAN_CIDR 必须是有效的网络 CIDR') from None
if not Path(sys.argv[4]).is_absolute() or any(c in sys.argv[4] for c in '\n\r"\\%'):
    raise SystemExit('SCAN_STATION_ENV_FILE 必须是有效的绝对文件路径')
PY

echo "== [1/5] packages =="
sudo apt-get update -qq
sudo apt-get install -y python3-impacket python3-pil python3-opencv \
    tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng

echo "== [2/5] application and local configuration =="
sudo mkdir -p /opt/scan-station "$(dirname "$ENV_FILE")"
for file in server.py configuration.py documents.py image_processing.py image_enhancement.py \
    scanner_status.py ocr_naming.py blank_pages.py index.html app.js style.css; do
  sudo install -m 644 "$SRC_DIR/$file" "/opt/scan-station/$file"
done
sudo chmod 755 /opt/scan-station/server.py
sudo python3 -m py_compile /opt/scan-station/server.py /opt/scan-station/configuration.py \
    /opt/scan-station/documents.py /opt/scan-station/image_processing.py /opt/scan-station/image_enhancement.py \
    /opt/scan-station/scanner_status.py /opt/scan-station/ocr_naming.py /opt/scan-station/blank_pages.py

if ! sudo test -e "$ENV_FILE"; then
  sudo install -m 600 "$SRC_DIR/.env.example" "$ENV_FILE"
  sudo python3 - "$ENV_FILE" "$PORT" <<'PY'
from pathlib import Path
import re
import sys
path = Path(sys.argv[1])
text = re.sub(r'^SCAN_STATION_PORT=.*$', "SCAN_STATION_PORT='" + sys.argv[2] + "'", path.read_text(), flags=re.M)
path.write_text(text)
PY
  echo "已创建不含凭据的本机配置模板；请填写扫描主机和账户后重启服务。"
fi
sudo chmod 600 "$ENV_FILE"

read_config_field() {
  sudo python3 - "$SRC_DIR" "$ENV_FILE" "$1" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from configuration import configured_port, read_environment_file
values = read_environment_file(sys.argv[2])
if sys.argv[3] == 'port':
    print(configured_port(values))
elif sys.argv[3] == 'data':
    print(values.get('SCAN_STATION_DATA_DIR') or '/var/lib/scan-station/documents')
else:
    raise SystemExit('不支持的配置查询')
PY
}
PORT="$(read_config_field port)"
DATA_DIR="$(read_config_field data)"
sudo mkdir -p "$DATA_DIR"

echo "== [3/5] systemd service =="
sudo tee /etc/systemd/system/scan-station.service >/dev/null <<EOF
[Unit]
Description=Scan Station web service
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/scan-station/server.py
Environment="SCAN_STATION_ENV_FILE=$ENV_FILE"
Restart=on-failure
RestartSec=3
WorkingDirectory=/opt/scan-station

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now scan-station
sudo systemctl --no-pager --full status scan-station

echo "== [4/5] firewall =="
if [ -n "$LAN_CIDR" ]; then
  if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow from "$LAN_CIDR" to any port "$PORT" proto tcp comment "scan station"
  else
    echo "未安装 UFW；请在主机防火墙中配置所选网段与端口。"
  fi
else
  echo "未设置 LAN_CIDR，没有创建防火墙放行规则。"
fi

echo "== [5/5] done =="
echo "网页端口：$PORT。请在可信网络中打开 http://<Linux 主机>:$PORT/"
