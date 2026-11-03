# CI / 上游同步说明

本仓库是 [meatRoll/gemini-web2api](https://github.com/meatRoll/gemini-web2api) 的 fork。
`main` 以上游为基底、用 rebase 保持线性历史，本地提交永远接在上游最新提交之上。

本地提交**不止 `.github/` 下的 CI 文件**，还有两处改动上游代码的修复，见文末「本地修复」表：

| 文件 | 说明 |
|------|------|
| `.github/workflows/docker.yml` | 镜像构建 + 推送 |
| `.github/workflows/sync-upstream.yml` | 每日同步上游 |
| `.github/SYNC.md` | 本文件 |
| `gemini_web2api/gemini.py` | Brotli 空响应修复（上游未合，PR #2 仍 open） |
| `tests/test_upstream_encoding.py` | 上述修复的回归测试 |

## 1. `docker.yml` — 编译并推送 Docker 镜像

- 触发：push 到 `main`、push `v*` 标签、手动 `workflow_dispatch`
- 平台：`linux/amd64` + `linux/arm64`
- 镜像：`ghcr.io/sofs2005/gemini-web2api`（public）
  - `main` 分支 → `latest` + `main` + `sha-xxxxxxx`
  - `v1.2.0` 标签 → `v1.2.0` + `1.2.0` + `latest`
- 构建前跑离线回归测试（`pytest`），构建后起容器做健康检查（不发上游生成请求）

拉取：

```sh
docker pull ghcr.io/sofs2005/gemini-web2api:latest
```

## 2. `sync-upstream.yml` — 每天 05:00 自动同步上游

- 触发：`schedule`（`0 21 * * *` UTC = **北京时间每天 05:00**）+ 手动 `workflow_dispatch`
- 流程：
  1. `git fetch upstream main`
  2. 上游无新提交 → 直接结束（不产生空提交）
  3. 有新提交 → `git rebase upstream/main`，把本仓库的本地提交接到上游最新提交之上（历史线性，无合并提交）；**冲突则中止同步并报错，绝不丢弃本地修复**
  4. 跑 `pytest` 回归，失败则不同步
  5. `push --force-with-lease` 到 fork 的 `main`
  6. 显式 `gh workflow run docker.yml` 兜底，保证 `latest` 一定被重建

改时间：编辑 `cron`（**UTC**，比北京时间减 8 小时）。北京时间 03:30 → `30 19 * * *`。
手动立刻同步：Actions → Sync upstream → Run workflow。

## 3. 本机自动更新（飞牛 NAS）

部署文件在飞牛目录，和改造前同一个位置，方便 NAS 端统一管理：

- `/vol1/1000/gemini-web2api/docker-compose.yml`（compose 项目名 `gemini2api`，旧文件备份为 `docker-compose.yml.bak-20261029`）
- 镜像直接拉 `ghcr.io/sofs2005/gemini-web2api:latest`，**不再 `build: .`**，本机源码不参与运行
- `watchtower`（`nickfedor/watchtower`，官方 `containrrr` 版客户端 API 太老，在这台机器上起不来）
  每 5 分钟检查一次，只更新带 `com.centurylinklabs.watchtower.enable=true` 的容器
- 配置/凭据仍挂载 `/vol1/1000/gemini-web2api/{config.json,cookie.txt,data}`，更新镜像不动数据

该目录属主是 uid 1000（飞牛用户），agent 账号无读权限，需要用 root 容器操作：

```sh
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  -v /vol1/1000/gemini-web2api:/vol1/1000/gemini-web2api \
  -w /vol1/1000/gemini-web2api \
  docker:cli compose -f /vol1/1000/gemini-web2api/docker-compose.yml up -d
```

## 冲突处理策略

**本地提交里有真正改上游代码的修复（`gemini.py`），所以冲突必须人工处理。**

- ⚠️ **禁止**用「以上游为准」（`-X theirs` / `checkout --theirs`）解冲突——那会把 Brotli
  修复冲掉，服务退回 **HTTP 200 + 空消息** 的静默失败，而且日志里看不出来。
- `sync-upstream.yml` 的策略是：rebase 冲突 → 工作流失败、不 push，本地修复完好保留。
  处理方式：本地 `git fetch upstream && git rebase upstream/main`，手工解冲突，跑
  `pytest tests/test_upstream_encoding.py`，再 `push --force-with-lease`，最后手动
  `gh workflow run docker.yml`。
- 判断冲突是否危险：只要 `gemini_web2api/gemini.py` 的 `_build_headers()`（`Accept-Encoding`）
  或 `generate()` 里的 `Content-Encoding` 检查被改动，就必须确认修复还在。
- 新写的本地改动尽量拆到独立文件（如 `.github/`、新模块），减少与上游争同一段代码。

## 历史说明

2026-10-29 之前 fork 里有 4 个 `deploy/serv00/panel4-*.sh`、`update-panel4-allowlist`
自用脚本（上游抽离 panel4 时未收录），以及 54 个与上游内容重复的旧提交。
已备份脚本到工作区 `work/gemini-web2api/panel4-backup/` 后把 `main` 重置为上游历史，
之后的同步都是 fast-forward，不再产生合并提交。

## 本地修复（fork 特有，不在上游）

| 修复 | 文件 | 原因 |
|------|------|------|
| `Accept-Encoding: identity` + 压缩响应显式报错 | `gemini_web2api/gemini.py` | 2026-10-29 起 Google 对带 `br, zstd` 的请求返回 Brotli，urllib 无法解码，表现为 **HTTP 200 + 空消息**。`tests/test_upstream_encoding.py` 守住这条。 |

同步时如与上游冲突，工作流会失败并保留本地修复——需要人工 rebase 后重跑。
