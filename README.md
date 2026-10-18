# Codex TPS Dashboard

一个只读的本地网页，用于分析 Codex Desktop 聊天的整轮耗时、首次输出等待和多种 TPS 指标。项目仅使用 Python 标准库，不需要 API Key，不发起模型调用，也不会上传日志。

> 当前适配 macOS 上的 Codex Desktop 本地日志结构。Codex 更新后，内部数据库或日志格式可能变化。

![Codex TPS Dashboard（演示数据）](docs/dashboard.jpg)

_截图使用脱敏演示数据，不包含真实聊天标题或本机日志。_

## 功能

- 按最近活动选择聊天，自动刷新时保留手动选择。
- 汇总后台整轮耗时、首次输出等待、整轮 TPS 和请求 TPS，并显示逐轮 P50。
- 区分模型请求耗时与请求外耗时，后者包含工具执行、请求准备、重试和客户端编排。
- 合并同一聊天拆分的 rollout 文件，并沿分叉历史还原当前可见轮次。
- 跨聊天、模型和思考强度查看汇总；排除子代理与审批聊天。
- 对缺失或无法可靠配对的日志显示 `—`，不猜测数据。
- 仅监听 `127.0.0.1`，数据库以只读模式打开。

## 环境要求

- macOS
- Codex Desktop，且本机已有聊天记录
- Python 3.9 或更高版本

项目没有第三方 Python 依赖。

## 快速开始

```sh
git clone https://github.com/zenvor/codex-tps-dashboard.git
cd codex-tps-dashboard
python3 codex_tps.py --open
```

也可以在 macOS Finder 中双击 `启动网页.command`。默认地址为 <http://127.0.0.1:8765>；终端按 `Ctrl+C` 停止服务，关闭网页不会停止后台进程。

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--codex-home PATH` | Codex 数据目录，默认为 `~/.codex` |
| `--thread UUID` | 首次打开时默认选择的聊天 |
| `--port PORT` | 本地监听端口，默认为 `8765` |
| `--open` | 启动后用默认浏览器打开页面 |
| `--export FILE` | 将所选聊天的统计结果导出为 JSON 后退出 |

示例：

```sh
python3 codex_tps.py --thread 00000000-0000-4000-8000-000000000001 --port 8766 --open
python3 codex_tps.py --codex-home /path/to/.codex
python3 codex_tps.py --thread 00000000-0000-4000-8000-000000000001 --export stats.json
```

网页还提供以下只读 JSON 接口：

- `/api/threads`：聊天列表
- `/api/stats?thread=<UUID>`：指定聊天的逐轮统计
- `/api/models`：跨聊天模型汇总

## 指标口径

- **后台整轮耗时**：优先直接采用 `task_complete.duration_ms`；旧日志缺少该字段时，回退到 `task_started` 与 `task_complete` 的事件时间差。它包含模型推理、工具执行、请求间准备、客户端编排和最终回答输出。
- **首次输出等待**：优先采用 `task_complete.time_to_first_token_ms`；旧日志只在请求与用量完整配对时，回退到第一条正文输出项的时间。
- **整轮 TPS**：本轮全部输出 Token ÷ 后台整轮耗时。输出 Token 包含隐藏推理及工具调用相关 Token，不等于可见文字速度。
- **请求耗时**：本轮可可靠配对的模型请求耗时总和。单次请求从 `stream_request` 认证记录计时至最后一个 `output_item_done`，属于近似边界。
- **请求外耗时**：后台整轮耗时 − 请求耗时，主要包括工具执行、下一次请求准备、重试和客户端编排。
- **请求 TPS**：本轮全部输出 Token ÷ 请求耗时，包含输入处理、等待、网络和推理时间。
- **正文 TPS 估计**：仅统计可以单独分离正文 Token 的请求，用正文输出项开始至完成的时间近似计算，并非首末 Token 的精确解码速度。

概览主值为已完成轮次的算术平均，小字中的 P50 为逐轮中位数。请求数与用量记录不一致、完成事件缺失、轮次未完成或被中断时，不计算对应速度。

### 与桌面端计时的差异

Codex Desktop 完成后显示的“工作了 X 分 X 秒”通常在最终回答开始时停止；本项目的后台整轮耗时包含最终回答输出，因此通常更长。本机 JSONL 没有保存桌面端使用的精确结束时间，本项目不会根据相邻事件伪造该数字。

## 数据来源与隐私

项目读取：

- `~/.codex/state_5.sqlite`：聊天信息和 rollout 路径
- `~/.codex/sessions/`、`~/.codex/archived_sessions/`：轮次、模型和 Token 记录
- `~/.codex/logs_2.sqlite`：请求和输出项事件

SQLite 数据库通过只读 URI 打开。服务不会修改 Codex 配置、认证信息或日志，也不会把数据发送到外部网络。

页面会显示聊天标题、模型、时间和性能数据。公开截图或导出的 JSON 前，请先确认其中没有不希望披露的信息。不要把本地服务转发到公网。

## 项目结构

```text
.
├── codex_tps.py          # 日志解析、统计和本地 HTTP 服务
├── index.html            # 单页仪表盘
├── test_codex_tps.py     # 统计、拆分日志和分叉历史测试
├── docs/dashboard.jpg    # 脱敏演示截图
└── 启动网页.command       # macOS 双击启动入口
```

## 验证

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_codex_tps.py
```

当前测试覆盖拆分 rollout 合并、分叉历史、Token 去重、旧日志回退、缺失事件、已中断轮次和各类汇总口径。

## 常见问题

### 端口已被占用

换一个端口启动：

```sh
python3 codex_tps.py --port 8766 --open
```

### 页面提示找不到聊天日志

确认 Codex Desktop 已产生本地聊天记录，并检查 `--codex-home` 是否指向正确的数据目录。

### 旧聊天没有请求 TPS 或正文 TPS

运行日志的保留时间通常短于聊天历史。旧聊天仍可显示 Token 和后台整轮耗时，但可能缺少请求级事件，页面会显示 `—`。

### 不同模型能否直接比较

耗时与 TPS 会受任务难度、工具数量、上下文、缓存和思考强度影响。比较模型时应固定这些条件；本项目提供的是本机实际任务统计，不是标准化基准测试。
