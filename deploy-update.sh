#!/usr/bin/env bash
# Apply a prepared update without initiating any scan.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 sudo bash deploy-update.sh" >&2
  exit 1
fi
SOURCE="$(cd "$(dirname "$0")" && pwd)"
TARGET=/opt/scan-station
ENV_FILE="${SCAN_STATION_ENV_FILE:-/etc/scan-station.env}"
BACKUP="/var/backups/scan-station/code-$(date +%Y%m%d-%H%M%S)"
export SCAN_STATION_ENV_FILE="$ENV_FILE"

read_config_field() {
  python3 - "$SOURCE" "$ENV_FILE" "$1" <<'PY'
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

python3 -c 'import cv2, numpy, PIL, impacket'
python3 -m py_compile "$SOURCE/server.py" "$SOURCE/configuration.py" "$SOURCE/documents.py" \
    "$SOURCE/image_processing.py" "$SOURCE/image_enhancement.py" "$SOURCE/scanner_status.py" \
    "$SOURCE/ocr_naming.py" "$SOURCE/blank_pages.py"
tesseract --list-langs 2>/dev/null | grep -qx chi_sim

# The same local environment file supplies the service and SMB preflight.
# Credential values are parsed as data and never printed.
python3 - "$SOURCE" "$TARGET" "$ENV_FILE" "$PORT" <<'PY'
import importlib.util
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, sys.argv[1])
from configuration import apply_environment_file
apply_environment_file(sys.argv[3])
base_url = 'http://127.0.0.1:' + sys.argv[4]
try:
    with urlopen(base_url + '/documents', timeout=10) as response:
        if json.load(response).get('active_id'):
            raise SystemExit('存在正在扫描的文件，暂不升级')
except HTTPError as exc:
    if exc.code != 404:
        raise
    legacy = Path(os.environ.get('SCAN_STATION_LEGACY_DIR') or '/tmp/scan-station')
    for folder in legacy.glob('*'):
        if not folder.is_dir():
            continue
        try:
            with urlopen(base_url + f'/scan/{folder.name}/status', timeout=5) as response:
                if json.load(response).get('state') == 'scanning':
                    raise SystemExit('存在正在扫描的批次，暂不升级')
        except HTTPError as status_error:
            if status_error.code != 404:
                raise
sys.path.insert(0, sys.argv[2])
spec = importlib.util.spec_from_file_location('deployed_station', Path(sys.argv[2]) / 'server.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
connection = module.smb()
try:
    pending = [item.get_longname() for item in connection.listPath('C$', module.REQ_DIR + '/*')
               if item.get_longname().lower().endswith('.json')]
    if pending:
        raise SystemExit('Windows 仍有待处理的扫描任务，暂不升级')
finally:
    connection.close()
PY

mkdir -p /var/backups/scan-station "$DATA_DIR"
cp -a "$TARGET" "$BACKUP"
echo "旧代码已备份。"

rollback() {
  trap - ERR
  echo "升级验证失败，恢复旧版服务；扫描文件目录保留。" >&2
  cp -a "$BACKUP/." "$TARGET/"
  systemctl restart scan-station
  exit 1
}
trap rollback ERR
systemctl stop scan-station
# Check persisted state after stopping to cover a batch started after preflight.
python3 - "$DATA_DIR" <<'PY'
import json
from pathlib import Path
import sys
for path in Path(sys.argv[1]).glob('*/document.json'):
    document = json.loads(path.read_text())
    if any(batch.get('pending') for batch in document.get('batches', [])):
        raise SystemExit('停服前收到新的扫描任务，恢复服务并延后升级')
PY
for file in server.py configuration.py documents.py image_processing.py image_enhancement.py \
    scanner_status.py ocr_naming.py blank_pages.py index.html app.js style.css; do
  install -m 644 "$SOURCE/$file" "$TARGET/$file"
done
chmod 755 "$TARGET/server.py"
if [ "${1:-}" = "--reprocess-pages" ]; then
  python3 "$SOURCE/tools/reprocess_documents.py" --data-dir "$DATA_DIR" > "$BACKUP/reprocess-results.json"
  python3 - "$BACKUP/reprocess-results.json" <<'PY'
import json
import sys
print('历史页面重新处理：', json.load(open(sys.argv[1]))['counts'])
PY
fi
systemctl start scan-station

python3 - "$PORT" <<'PY'
import json
import sys
import time
from urllib.request import urlopen

base_url = 'http://127.0.0.1:' + sys.argv[1]
for attempt in range(30):
    try:
        with urlopen(base_url + '/documents', timeout=3) as response:
            documents = json.load(response)
        with urlopen(base_url + '/scanner/status', timeout=3) as response:
            scanner = json.load(response)
        print(json.dumps({'history_files': len(documents['documents']),
                          'scanner_state': scanner.get('state')}, ensure_ascii=False))
        break
    except Exception:
        if attempt == 29:
            raise
        time.sleep(1)
PY
trap - ERR
echo "已更新，网页端口：$PORT。"
