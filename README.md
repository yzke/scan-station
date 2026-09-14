# Scan Station

浏览器中的扫描文件工作台。Windows 电脑通过 WIA 驱动连接扫描仪，Linux 服务通过 SMB 接收页面、裁切黑边、保存文件记录，并导出 PDF。

## 功能

- 网页优先选择 **300 DPI**；设备报告支持后可使用 150、200、300 DPI。能力尚未确认时保守使用 150 DPI，单双面选项也以设备报告为准。
- 新文件与继续扫描分开操作，页面逐张显示并持久保存。扫描中断后保留已接收页面，重启服务不会自动重新走纸。
- 文件记录每页 **10 条**，支持打开、重命名、下载和删除文件。删除会隐藏文件并保留磁盘图片，不用于释放磁盘空间。
- 文件可选择 **原图／增强／黑白**，预览和 PDF 使用同一模式。这里的“原图”是已经裁切、尚未额外调色的基图；未经处理的 raw 扫描原件仍可单独查看。
- 增强模式校正纸面亮度并保留彩色内容；黑白模式将页面转为黑白。切换模式不改写基图、raw 或已有页面版本。
- 首张页面可通过本机 OCR 自动命名。手动编辑名称会停止后续自动改名；OCR 与空白页分析始终读取裁切基图。
- 支持调整页序、重扫单页、标记疑似空白页、删除选中页，以及在页面未进一步改变时撤销这次批量删除。

## 部署条件

Linux 端需要 Python 3.10 或更新版本，以及 OpenCV、NumPy、Pillow、Impacket。自动命名使用 Tesseract 和 `chi_sim+eng` 语言包。Windows 端需要 PowerShell 5.1、可用的 WIA 扫描驱动，以及已配置好的 SMB 访问权限。

当前实现使用 Windows 的 `C$` 共享访问 `Users/Public/scan-agent/`。请使用你自己的 Windows 主机地址和有权访问该目录的账户；项目不会配置 Windows 共享权限或创建账户。Windows 安装和只读能力检查见 [Windows 说明](windows/README.md)。

**网页服务目前没有登录功能。** 只应在可信局域网中使用，或放在带身份认证的反向代理后面；不要直接暴露到公网。部署前按实际需要配置主机防火墙。

## Linux 安装

`install.sh` 面向使用 apt、systemd 的 Linux 系统。它安装系统依赖、部署到 `/opt/scan-station`，并从模板创建本机配置文件；已有配置文件会保留。

```bash
bash install.sh
sudoedit /etc/scan-station.env
sudo systemctl restart scan-station
```

把配置中的 `SCAN_STATION_SCANNER_HOST`、`SCAN_STATION_SMB_USER`、`SCAN_STATION_SMB_PASSWORD` 填为你自己的连接信息。使用单行 `KEY=value` 格式；值包含空格、`$` 或 `#` 时请加引号。不要把完成后的环境文件提交到仓库或附在问题报告中。

可在安装时指定本机环境文件路径、首次创建配置时的端口，以及允许访问网页的局域网 CIDR：

```bash
SCAN_STATION_ENV_FILE=/etc/scan-station.env PORT=8081 \
  LAN_CIDR='<你的可信局域网 CIDR>' bash install.sh
```

请先将占位符替换为实际网段。未提供 `LAN_CIDR` 时，安装脚本不会创建防火墙放行规则；如使用 UFW 或其他防火墙，请自行确认规则。已有环境文件里的端口是服务、升级预检和健康检查的共同配置来源。

配置字段：

| 字段 | 用途 |
| --- | --- |
| `SCAN_STATION_SCANNER_HOST` | Windows 扫描主机的地址或名称，必须填写 |
| `SCAN_STATION_SMB_USER` | 本机维护的 Windows SMB 账户，必须填写 |
| `SCAN_STATION_SMB_PASSWORD` | 对应凭据，必须填写且仅保存在本机 |
| `SCAN_STATION_LISTEN_HOST` | 网页监听地址，默认 `0.0.0.0`；同机反向代理可使用 `127.0.0.1` |
| `SCAN_STATION_PORT` | 网页端口，默认 `8081` |
| `SCAN_STATION_DATA_DIR` | 文档持久目录，默认 `/var/lib/scan-station/documents` |
| `SCAN_STATION_LEGACY_DIR` | 可选旧数据导入目录，默认 `/tmp/scan-station` |

浏览器打开 `http://<Linux 主机>:<配置端口>/`。缺少扫描连接配置时，已有历史文件仍可读取；尝试连接扫描电脑会显示缺少的配置项名称。

## 更新

把新版源文件放入独立目录，在确认没有扫描任务后运行：

```bash
sudo env SCAN_STATION_ENV_FILE=/etc/scan-station.env bash deploy-update.sh
```

升级脚本读取同一份本机环境文件，确认网页和 Windows 请求队列空闲后备份代码、停服更新并检查恢复情况。失败时恢复旧代码。升级不会提交扫描请求，文档目录不会被代码升级覆盖。

## 数据与图像

每份文件使用固定 ID 子目录保存 `document.json`、raw 原件、裁切基图和不可变页面版本。重命名只改变元数据。`page_order` 保存页序，页面 `revision` 标识当前图像版本；重扫成功后原子切换版本指针，旧图片仍保留。

增强图片在需要预览或导出时生成，按算法版本、页面版本及模式缓存在文件目录内。缓存可重建，不修改原件、页序或文档更新时间。算法说明和参考来源见 [图像增强](docs/image-enhancement.md)。

离线重处理裁切效果前，应备份文档目录并停止网页服务。工具拒绝处理存在未结束扫描的目录：

```bash
python3 tools/reprocess_documents.py --data-dir '<文档目录>' --dry-run
python3 tools/reprocess_documents.py --data-dir '<文档目录>'
```

## 本地开发与验证

仓库只包含源码与合成测试夹具，不包含扫描样张、用户文档、日志或部署凭据。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

Python 测试使用临时目录、合成 JPEG 和假的 SMB 协调器。部分 API 测试需要创建本地回环 HTTP 套接字。Windows 脚本的模拟检查命令见 Windows 说明。

无需扫描仪的本地演示会现场生成两张合成页面：

```bash
.venv/bin/python tools/image_only_demo.py --port 18081
```

其他离线工具可处理用户自己指定的本机 PDF 或图片目录，输出不会自动加入版本控制。提交问题报告前，请移除文档名称、设备信息、路径及凭据；不要上传实际扫描件。
