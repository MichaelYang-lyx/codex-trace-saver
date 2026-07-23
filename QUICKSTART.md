# codex-trace-saver 用法

## 装(一次)

```bash
curl -fsSL https://raw.githubusercontent.com/MichaelYang-lyx/codex-trace-saver/main/install.sh | bash
```

装完 `codex-save-trace` 就在你的 PATH 上了。若 `~/.local/bin` 不在 PATH,脚本会提示你怎么加。

## 用

**在任意终端里跑**(也可以在 codex 会话里让 agent 帮你跑):

```
codex-save-trace              先预览:会传哪个 rollout + 附带哪些文件
codex-save-trace --yes        确认,直接上传到排行榜(+1 分)
codex-save-trace --yes --local  只存本地,不上传 → ~/codex-traces/
```

**微调附带的文件(和 --yes 一起用):**
```
codex-save-trace --yes -x debug.log        去掉某个文件
codex-save-trace --yes -a extra.csv        再补一个文件
codex-save-trace --yes --only *.xlsx       只保留 xlsx
codex-save-trace --yes --no-files          只传 rollout,不带文件
```

**列出最近的 rollouts:**
```
codex-save-trace list          # 15 个
codex-save-trace list 30       # 30 个
codex-save-trace --yes <rollout-id-substring>   # 指定一个上传
```

- `-x`=排除,`-a`=补充,`--only`=白名单。可重复,支持通配符。
- 自动跳过 `.env` / `*.key` / SSH 密钥、大于 50MB 的文件、`.git` / `.codex` / `node_modules` 等目录。

**看榜**: <http://10.9.66.12:8848>

**改名字(可选)**:
```bash
export TRACE_LEADERBOARD_NAME="你的名字"
```

## 让 codex agent 帮你跑

Codex 没有斜杠命令机制,但 agent 能跑 shell。会话里直接说:

> 帮我把这次的 trace 和文件都保存到排行榜

agent 会执行 `codex-save-trace --yes`。
