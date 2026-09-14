# Windows 扫描代理

在连接扫描仪的 Windows 电脑上安装 WIA 驱动，并确认运行代理的账户可以访问扫描仪。Linux 服务需要通过 SMB 访问这台电脑的 `C$` 共享及 `Users/Public/scan-agent/`；共享权限和连接凭据由使用者在本机配置。

## 首次安装

打开 PowerShell，进入仓库的 `windows` 目录，将运行文件复制到固定工作目录：

```powershell
$agentDirectory = 'C:\Users\Public\scan-agent'
New-Item -ItemType Directory -Force -Path $agentDirectory | Out-Null
Copy-Item .\scan-agent.ps1, .\scanner-wia.ps1, .\wia-batch.cs, .\run-agent.cmd -Destination $agentDirectory
```

确认没有另一个代理进程后，双击工作目录中的 `run-agent.cmd`。代理会创建 `req`、`out` 目录并持续等待网页任务；首次安装不会自动创建登录启动项。若目录已有旧代理，使用下方更新流程，不要直接覆盖运行中的文件。

## 更新

在 Windows 电脑上解压完整新版 `windows` 目录，确认没有人在网页发起扫描，然后双击 **update-agent.cmd**。完成后刷新网页。更新过程不会提交扫描任务。

更新入口会校验完整脚本、检查 `C:\Users\Public\scan-agent\req` 没有任务、备份现有代理，然后停止对应的 PowerShell 进程、更新并重新启动。备份位于 `C:\Users\Public\scan-agent\backups\时间戳`，原扫描图片和请求目录会保留。如果提示访问被拒绝，使用运行原扫描代理的 Windows 账号执行，不修改系统权限或安全设置。

运行时文件是 `scan-agent.ps1`、`scanner-wia.ps1`、`wia-batch.cs` 和 `run-agent.cmd`。v5 在 WIA 2 送纸器项上设置整批页数 `WIA_IPS_PAGES=0`，仅调用一次 `IWiaTransfer.Download`，由回调依次接收各页，避免脚本逐页重新发起扫描任务。优先选择驱动支持的原生 JPEG；仅提供 BMP 时，回调将已关闭并刷新的文件交给独立 JPEG 线程，队列最多保留八个文件路径，不把设备对象或整批图像放进后台线程。终态等待所有转换和发布完成；成功 JPEG 发布后清理对应临时原始流。

请求可包含 `dpi: 150|200|300` 和 `duplex: true|false`；协议默认 150 DPI 单面，网页在设备支持时优先选择 300 DPI。重扫本页专用的 `max_pages: 1` 强制单面、设置 `WIA_IPS_PAGES=1` 并读回验证；回调也独立拒绝第二个页面流。不带此字段的普通批次处理送纸器中的全部页面。双面模式中的页数按图像面计算；纸张裁切和纠偏在 Linux 主机上完成。

只有在整批接口初始化或设置失败、尚未调用 `Download` 时，才可退回原来的逐页模式，网页显示“逐页兼容模式”。部分设置已改动时，兼容路径必须完整重设清晰度、单／双面、扫描区域和每次传输页数，无法安全重设则直接报错。调用过 `Download` 后，无论保存了几页、是否返回错误，都不会改用另一条路径重扫。实际走纸速度仍取决于驱动对整批扫描的支持。

成功终态为 `ok:页数`；故障为 `error pages=已保存页数: 消息`。只有已有完整页面后遇到真正的 WIA 空纸错误才算正常结束；卡纸、多页送入、通信或 JPEG 保存失败均报告错误并保留已有页面。JPEG 到达正式文件名后才计入完成页数。首次传输前持久化 `.started` 标记；重启遇到中断批次时保留图片并报告中断，不会自动重扫或删除页面。

状态写入独立的 `C:\Users\Public\scan-agent\scanner-status.json`，不会写进 `req`。v5 心跳包含 `agent_version: 5` 与 `supports_page_rescan: true`；Linux 在新鲜的 v4 或更新心跳下开放重扫本页。`capture_mode` 为 `wia2-preferred`（等待实际请求确认）、`wia2-batch`（已选择原生整批路径）或 `wia-automation-compat`（逐页兼容）。空闲时每十秒枚举 WIA 扫描仪并读取属性；扫描回调限频更新忙碌心跳，状态文件暂时写失败不会中止正在扫描的纸张。心跳缺失或过期表示“暂时无法确认”，不作为关机证据。

## 不扫描的检查

以下命令仅解析脚本／执行模拟属性测试，不连接硬件：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scan-agent.ps1 -ValidateOnly
powershell -NoProfile -ExecutionPolicy Bypass -File .\test-wia.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\test-wia-batch.ps1
```

`scan-agent.ps1 -StatusOnly` 只读取 WIA 设备和能力并写出状态，不进入扫描请求循环。仅在没有正在进行的扫描时使用，避免与扫描驱动并发连接。`probe-scanner.ps1` 只读出更完整的驱动属性，同样不传输图像。

## 验证范围与恢复

v5 使用 PowerShell 5.1 编译 C# 并加载完整脚本。测试覆盖整批仅一次下载、逐页 JPEG 发布、最后一流结束、双重故障优先级、取消、不完整 JPEG／BMP、发布冲突、兼容设置恢复和重启保留已有文件。测试只使用模拟属性、模拟回调和临时图片，不实例化原生设备会话，也不能替代具体扫描仪的驱动兼容性验证。

异常时 `out` 内的 `.capture`／`.capture.part` 保留已经接收的原始流，供恢复和排查；已有正式 JPEG 不会删除或覆盖。成功发布后不会长期保留第二份完整 BMP。`S_FALSE`／取消／卡纸均报错，只有普通整批在已有完整页后遇到真正空纸才正常结束。

如需恢复，先停止这个扫描代理进程，并确认 `req` 没有任务；将对应备份中的 `scan-agent.ps1`、`run-agent.cmd`（以及若存在的 `scanner-wia.ps1`、`wia-batch.cs`）复制回主目录，再运行主目录的 `run-agent.cmd`。不要同时启动两个代理。

驱动定义依据：[WIA 分辨率](https://learn.microsoft.com/en-us/windows-hardware/drivers/image/wia-ips-xres)、[扫描仪能力](https://learn.microsoft.com/en-us/windows-hardware/drivers/image/wia-dps-document-handling-capabilities)、[WIA 属性子类型](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/wiaaut/-wiaaut-wiasubtype)、[Microsoft WiaDef.h 常量](https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/WiaDef.h)。

批量传输依据：[Microsoft 数据传输示例](https://github.com/microsoft/Windows-classic-samples/blob/main/Samples/Win7Samples/multimedia/wia/datatransfer/DataTransfer.cpp)、[SDK COM 接口定义](https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/wia_lh.h)、[整批／单页设置](https://learn.microsoft.com/en-us/windows-hardware/drivers/image/wia-ips-pages)、[传输回调常量](https://learn.microsoft.com/en-us/windows-hardware/drivers/image/wia-transfer-constants)。
