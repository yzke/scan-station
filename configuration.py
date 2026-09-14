"""Read local key/value configuration without evaluating shell code."""
import os
from pathlib import Path
import re
import shlex


CONFIG_KEYS = {
    "SCAN_STATION_SCANNER_HOST", "SCAN_STATION_SMB_USER", "SCAN_STATION_SMB_PASSWORD",
    "SCAN_STATION_LISTEN_HOST", "SCAN_STATION_PORT", "SCAN_STATION_DATA_DIR", "SCAN_STATION_LEGACY_DIR",
}


def read_environment_file(path):
    """Accept one KEY=value per line, with optional quotes and comments.

    Values are never evaluated as shell code. Use single-line quoted values
    for spaces, dollar signs, or comment characters in credentials.
    """
    values = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        try:
            fields = shlex.split(line, comments=True, posix=True)
        except ValueError:
            raise ValueError(f"环境文件第 {number} 行引号未闭合") from None
        if not fields:
            continue
        if len(fields) != 1 or "=" not in fields[0]:
            raise ValueError(f"环境文件第 {number} 行须为 KEY=value")
        key, value = fields[0].split("=", 1)
        if key not in CONFIG_KEYS:
            raise ValueError(f"环境文件第 {number} 行包含不支持的配置项")
        values[key] = value
    return values


def apply_environment_file(path=None):
    path = path or os.environ.get("SCAN_STATION_ENV_FILE")
    if path:
        os.environ.update(read_environment_file(path))


def configured_port(values=None):
    value = (os.environ if values is None else values).get("SCAN_STATION_PORT", "8081")
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,4}", value) or int(value) > 65535:
        raise ValueError("SCAN_STATION_PORT 必须是 1–65535 的整数")
    return int(value)
