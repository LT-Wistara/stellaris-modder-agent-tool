# 0.1.3 发布审阅

安装、Mod 路径和许可说明见 [README](../README.md)、[Windows 发布版说明](WINDOWS_RELEASE.md) 和 [LICENSE](../LICENSE)。

该工具将 CWT 规则、原版游戏和当前 Mod 的声明统一索引，通过 CLI、Python API
及 MCP 提供带证据等级的查询和验证。保留纯标准库运行时、查询与验证共用索引的设计，
本次增加 Windows x64 EXE 便携包。

## 已修复

- CLI 文档中的 `search --mode text` 原先被 argparse 拒绝；现转发至已有文本检索实现。
- 管道启动下 `--print-only` 原先进入 stdio 服务，不打印配置；现始终打印配置后退出。
- 启动器缺少 `--game-root` / `--mod-root`；现支持显式路径并优先于环境变量。
- 端口探测把任意成功响应或 403 等状态当成可用 MCP 服务，而且只读取前 400 字节；
  现读取并校验本工具的 serverInfo，其他服务占端口时正常报告监听失败。
- stdio 启动失败的自然语言错误原先写入 stdout，破坏 JSON-RPC；现写 stderr。
- IPv6 `::1` 原先使用 IPv4 socket，地址也未加方括号；现使用 IPv6 socket 和正确 URL。
- SSE 有限时长结束时原先保持无长度 HTTP/1.1 连接，客户端无法知道流结束；现明确关闭连接。
- 进度条测试依赖连续读取计时器得到不同值，在 Windows 上偶发失败；使用固定测试时钟，
  保留程序在尚不能估算速度时不显示剩余时间的行为。

## EXE 发布适配

- Mod 目录由用户显式指定，不从 EXE 所在位置自动识别。
- 配置直接调用 EXE 的 `serve`；诊断命令通过 `EXE cli ...` 使用。
- 打包全部 CWT、清单、许可及版本文件；包含 Python 和 SSL 运行库。
- 选择 PyInstaller 目录式便携包：更新语料持久保存在 `_internal`，启动无需临时解压。
- 源码 ZIP 排除构建环境、Releases 和日志，避免把依赖或发布产物再次打包。

## 实现方案建议

当前单人本机工作流无需引入 Web 框架或数据库。目录式 EXE 对可更新语料更合适，
比单文件 EXE 加用户缓存同步更简单。

后续若扩展为多人远程服务，建议先将动态索引重建改为构造完整快照后整体切换。
目前 HTTP 使用线程共享 Database，而动态注册表刷新会逐项清空并重建缓存；
现有锁主要保护 Database.dynamic 入口，并非整个查询期间的快照一致性保证。
此外，热更新指纹只覆盖定义目录，本地化缓存应增加独立失效机制，使仅修改翻译时
中文名字查询也能及时刷新。以上为后续改进项，本次未改动索引架构。

## 验证

源码回归结果见 TEST_REPORT.txt / TEST_REPORT.json；173 个 CWT 全部通过无损解析。
`scripts/smoke_release.py` 会将 ZIP 解压到独立中文含空格目录，清空 PATH 后验证
CLI、手动指定 Mod 目录、stdio 四个工具和 HTTP 握手/检索，确认运行的是实际发布 EXE。
此验证检查工具行为，不替代 Stellaris 游戏内验证。

本次实际 EXE 验证通过：Windows 11 x64 / Python 3.13.9 / PyInstaller 6.16.0，
中文含空格目录迁移、空 PATH、173 文件 roundtrip、EXE 配置、手动指定 Mod 目录、
stdio 全部四个工具及 HTTP 握手/列举/检索均通过。另通过本地 HTTP 请求确认
SSE 到期后客户端能读到 EOF。
