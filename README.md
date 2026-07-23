# codex-trace-saver

**Codex 版的 trace-saver** —— 一键把当前 Codex 会话的 rollout + 会话里读/写过的文件打包上传到排行榜(和 [`hermes-trace-saver`](https://github.com/MichaelYang-lyx/hermes-trace-saver) 走同一个服务、同一套评分)。

## 一键装

```bash
curl -fsSL https://raw.githubusercontent.com/MichaelYang-lyx/codex-trace-saver/main/install.sh | bash
```

不需要 root。装完把 `codex-save-trace` 放到 `~/.local/bin/`。

## 用

```
codex-save-trace              # 预览
codex-save-trace --yes        # 上传(+1 分)
codex-save-trace --yes --local  # 只本地打包,不上传
codex-save-trace list         # 列出最近的 rollouts
codex-save-trace --help       # 完整帮助
```

微调、指定 rollout、改榜名等详细用法见 [QUICKSTART.md](QUICKSTART.md)。

## 与 hermes-trace-saver 的差异

| | hermes-trace-saver | codex-trace-saver |
|---|---|---|
| 集成形态 | Hermes 原生插件,`/save-trace` 斜杠命令 | 独立 CLI,`codex-save-trace` |
| 会话数据源 | `~/.hermes/state.db`(SQLite) | `~/.codex/sessions/**/rollout-*.jsonl` |
| 当前会话定位 | `HERMES_SESSION_ID` / state.db 最新活跃 | `CODEX_SESSION_ID` / rollout 文件 mtime 最新 |
| 相对路径解析 | `/proc/*/cwd` 找 hermes 进程 | `/proc/*/cwd` 找 codex 进程 |

**上传格式完全一致**(`sessions/` + `files/` + `manifest.json` 的 zip),所以两者在排行榜上无差别。

## 依赖

优先用 `requests`,没有则退回标准库 `urllib`。**零额外依赖**。

## 文件

```
codex-trace-saver/
├── codex-save-trace       # CLI 入口(可执行)
├── uploader.py            # 找 rollout、打 zip、登录 + 上传
├── filepicker.py          # 扫描 rollout、过滤文件、生成预览
├── install.sh             # 一键安装
├── uninstall.sh           # 一键卸载
├── config.example.env     # 环境变量样例
├── QUICKSTART.md
└── README.md
```

## 配置(环境变量,都可选)

| 变量 | 默认 | 说明 |
|------|------|------|
| `TRACE_LEADERBOARD_NAME` | 系统用户名 | 榜上显示名 |
| `TRACE_LEADERBOARD_URL` | `http://10.9.66.12:8848` | 排行榜地址 |
| `TRACE_SAVE_DIR` | `~/codex-traces` | `--local` 模式的输出目录 |
| `CODEX_HOME` | `~/.codex` | Codex 主目录 |
| `CODEX_SESSION_FILE` | (未设) | 显式指定当前 rollout 路径 |
| `CODEX_SESSION_ID` | (未设) | 显式指定 rollout id(会自动匹配文件名) |

设置示例:

```bash
export TRACE_LEADERBOARD_NAME="张三"
```

## 状态

- Codex rollout 格式基于 OpenAI Responses API 事件流(每行一个 JSON:`function_call` / `function_call_output` / `message` 等)。
- 扫描器同时读 `function_call` 参数**和** `function_call_output` 内容,和 hermes 版本对齐。
- 未做真机验证——请你在有 codex 的机器上先 `codex-save-trace --yes --local` 检查一遍 zip 内容,没问题再真上传。
