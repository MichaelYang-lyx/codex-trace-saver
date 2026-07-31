# codex-trace-saver 快速教程

一步一步跟着做即可 —— 5 分钟就能把 codex 的会话 + 用过的文件传到排行榜。

如果你已经装过、想直接看命令表,直接跳到最下面的[命令速查](#命令速查)。

---

## 前置条件

- **你已经装好 codex** 且能跑一次 `codex exec`(见 [Codex 官方文档](https://github.com/openai/codex))。
- 能访问 http://10.9.66.12:8848(排行榜服务)。

如果 `codex --version` 没反应,先装:
```bash
npm install -g @openai/codex
```

---

## 第 1 步:安装

一行搞定:

```bash
curl -fsSL https://raw.githubusercontent.com/MichaelYang-lyx/codex-trace-saver/main/install.sh | bash
```

装完你的 PATH 上会有一个 `codex-save-trace` 命令。

**验证一下**:
```bash
codex-save-trace --help
```

若提示 `command not found`,把这行加进 `~/.bashrc` / `~/.zshrc`:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

---

## 第 2 步:改个榜名(可选,推荐)

不设的话会用系统用户名。想在榜上显示中文名/花名:

```bash
export TRACE_LEADERBOARD_NAME="张三"
```

写进 shell rc(`~/.bashrc` 之类)长期生效。

---

## 第 3 步:先跑一次 codex 会话

在 codex 里正常聊几句,让它**真的读/写一些文件**(不然没什么可以打包的):

```bash
codex exec --skip-git-repo-check "读一下 /path/to/report.md 有多少字"
```

或者在交互式 `codex` 里让它跑 shell、读文件、写文件都可以。

---

## 第 4 步:预览一下会传什么

```
codex-save-trace
```

你会看到类似:

```
rollout: rollout-2026-07-29T16-03-17-....jsonl  [最新 rollout (按 mtime)]
attach files: 1 file(s), ~0.2 KB
  ✓ /path/to/report.md  (183 B)
skipped: 2 file(s)
  ✗ /somewhere/scratch  — missing or not a regular file
  ✗ /tmp/foo.env  — sensitive filename

Add --yes to upload  (or --yes --local to save the zip locally).
```

- `✓` 的是会被打包上传的文件
- `✗` 的是被跳过的(不存在 / 是目录 / 敏感文件 / 太大)

自动跳过的东西:`.env`、`*.key`、`*.pem`、SSH 密钥,大于 50MB 的文件,以及 `.git` / `.codex` / `node_modules` / `.claude` 等目录里的文件。

---

## 第 5 步:确认上传

```
codex-save-trace --yes
```

看到这样就成功了:

```
📤 Uploaded rollout + 1 file(s) as '张三' (11.9 KB). See http://10.9.66.12:8848/u/张三
```

打开榜看看:<http://10.9.66.12:8848>,你名字应该 **+1 分**了。

---

## 常见微调(第 5 步的变体)

**只想传某几个文件,自己指定**(跳过 rollout,只传文件):
```
codex-save-trace --yes --no-files -a input.xlsx -a output.xlsx
```

**扫描后想再排除一个**:
```
codex-save-trace --yes -x debug.log
```

**扫描后想再补一个**:
```
codex-save-trace --yes -a extra.csv
```

**只保留特定文件**:
```
codex-save-trace --yes --only "*.xlsx"
```

**只传 rollout,不带任何文件**:
```
codex-save-trace --yes --no-files
```

**只在本机打包(不上传)**:
```
codex-save-trace --yes --local
# → 存到 ~/codex-traces/ (可用 TRACE_SAVE_DIR 覆盖)
```

**上传别的 rollout(不是最新的)** — 两种方式:

**方式 1(推荐):`--pick` 交互式选择**
```
codex-save-trace --pick               # 弹出菜单选一个 → 预览
codex-save-trace --pick --yes         # 弹出菜单选一个 → 直接上传
codex-save-trace --pick --yes --local # 选中后打包到本地
```

菜单里每行显示 **时间 · 大小 · cwd · 消息数 · 首个 user 问句**,一眼认出这个 session 是聊什么的。真终端下 ↑/↓ 或 j/k 移动、Enter 确认、q/Esc 取消;管道场景自动 fallback 到数字选择。

**方式 2:手动看 list + 指定 id 片段**
```
codex-save-trace list                    # 看最近的 rollouts
codex-save-trace --yes 019face3          # id 片段能唯一识别就行
```

---

## 让 codex 自己帮你跑

Codex 没有斜杠命令,但 agent 能跑 shell。在会话里直接说:

> 帮我把这次的 trace 和文件都保存到排行榜

它会执行 `codex-save-trace --yes` 完成上传。

---

## 命令速查

| 想做 | 命令 |
|------|------|
| 预览会传什么 | `codex-save-trace` |
| 确认上传 | `codex-save-trace --yes` |
| 只存本地,不上传 | `codex-save-trace --yes --local` |
| **交互式选一个 rollout** | `codex-save-trace --pick` / `--pick --yes` |
| 列出最近 rollout | `codex-save-trace list` |
| 上传指定 rollout | `codex-save-trace --yes <id-片段>` |
| 打包全部 rollout(一个 zip、+1) | `codex-save-trace --yes all` |
| 打包全部 rollout(每个 +1) | `codex-save-trace --yes all --split` |
| 排除某文件 | `codex-save-trace --yes -x <名/glob>` |
| 追加某文件 | `codex-save-trace --yes -a <路径>` |
| 只要某类文件 | `codex-save-trace --yes --only <glob>` |
| 只传 trace 不带文件 | `codex-save-trace --yes --no-files` |
| 加备注 | `codex-save-trace --yes -n "本周实验"` |
| 详细模式(显示跳过) | `codex-save-trace -v` |
| 完整帮助 | `codex-save-trace --help` |

---

## 环境变量(全都可选)

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TRACE_LEADERBOARD_NAME` | 系统用户名 | 榜上显示名 |
| `TRACE_LEADERBOARD_URL` | `http://10.9.66.12:8848` | 排行榜地址(换服务器改这个) |
| `TRACE_SAVE_DIR` | `~/codex-traces` | `--local` 模式的输出目录 |
| `CODEX_HOME` | `~/.codex` | codex 数据主目录 |
| `CODEX_SESSION_FILE` | (未设) | 显式指定要打包的 rollout jsonl 路径 |

---

## 出错了怎么办

| 现象 | 排查/解决 |
|------|-----------|
| `⚠️ No Codex rollout found under ~/.codex/sessions` | codex 还没跑过任何会话,先在 codex 里聊几句 |
| `⚠️ Leaderboard unreachable: ...` | 网络到不了 `10.9.66.12:8848`,或服务挂了。`curl http://10.9.66.12:8848/healthz` 测一下 |
| 上传成功但榜上找不到我 | 打开 <http://10.9.66.12:8848/u/你的名字> 直接看 |
| 想升级 | 再跑一次 `curl \| bash` 就行,幂等 |
| 想卸载 | `bash ~/.local/share/codex-trace-saver/uninstall.sh` |

---

## 一句话给同事的版本

> Codex 版本 trace-saver:
> `curl -fsSL https://raw.githubusercontent.com/MichaelYang-lyx/codex-trace-saver/main/install.sh \| bash`
> 装完 `codex-save-trace --yes` 就把当前 codex 会话 + 用过的文件传到 <http://10.9.66.12:8848> 上榜。
