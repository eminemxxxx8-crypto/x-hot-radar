# 调度诊断与免费方案边界

## 已检查的实际记录

检查了现有 `radar.py`、`.github/workflows/radar.yml`、`data/state.json`、相关 Git 历史，以及 GitHub 公开 [工作流运行列表](https://github.com/eminemxxxx8-crypto/x-hot-radar/actions/workflows/radar.yml)。GitHub API 在当前云环境被代理 403 拒绝，因此使用公开网页的结构化运行数据核对，没有读取 Secrets 或提取登录凭据。

检查时页面共有 17 次运行：14 次 Scheduled、2 次手动、1 次初始代码提交。14 次定时记录中 13 次成功、1 次失败。相邻定时运行创建时间间隔约 **199.7～432.5 分钟**。部分最近记录（北京时间）：

| 运行 | 创建时间 | 结果 | 持续时间 |
| --- | --- | --- | --- |
| [#17](https://github.com/eminemxxxx8-crypto/x-hot-radar/actions/runs/41088148921) | 2026-10-31 00:15:05 | 成功 | 15 秒 |
| [#16](https://github.com/eminemxxxx8-crypto/x-hot-radar/actions/runs/41034951191) | 2026-10-30 17:38:52 | 成功 | 14 秒 |
| [#15](https://github.com/eminemxxxx8-crypto/x-hot-radar/actions/runs/40994717051) | 2026-10-30 10:58:24 | 成功 | 14 秒 |
| [#14](https://github.com/eminemxxxx8-crypto/x-hot-radar/actions/runs/40974369173) | 2026-10-30 07:37:12 | 成功 | 15 秒 |

旧工作流的 `4,19,34,49 * * * *` 是合法的每 15 分钟 cron，且已经避开整点。通常 11～31 秒的执行时长不足以解释几小时的触发间隔；原配置的 8 分钟 timeout 也不是几小时的队列占用。网页确实反映了触发稀疏，而不只是 Git 提交少。

当前证据**不能说明 GitHub 为什么没创建其余预期运行**，也无法从运行列表证明某个未生成的任务具体是延迟、丢弃还是其他平台策略造成的。#10 失败页只公开了 exit code 1；没有获取其任务日志，不能声称已确定该次失败原因。原本“仅在状态变更时提交”还会让 Git 提交次数和执行次数不同，但这不能解释列表本身的稀疏。

## 这次采取的措施

- cron 改为 `7,17,27,37,47,57 * * * *`，每 10 分钟请求一次，增加尝试机会并避开整点。**无法据此保证实际频率改善**；合并后应继续观察至少一天。
- 4 个工作线程并发读取 10 个 RSS 请求，保留请求超时及有限重试，减少串行等待。Telegram 不自动重试，避免不明确超时时盲目重复。
- 手动触发默认执行正常采集，`send_test` 默认 false，避免把手动补查变成只发连接测试。
- 运行 Summary 记录当前启动时间和距离上次成功数据运行的分钟数；状态增加 `last_run`。它显示观察到的空档，不伪称知道未触发任务的原因。
- 生产任务串行，启动后检出最新 `main`；每次发送成功立即本地记账，即使后续发送失败也尝试提交已保存状态。推送采用普通 rebase/push，发生冲突即失败，不覆盖分支。
- PR / push 只做测试；生产任务通过事件和 main 分支条件保护，避免测试分支重复推送 Telegram。

## 合并后怎么确认

1. 手动正常运行一次（`main`, `send_test=false`），确认测试 job、采集 job 和状态提交成功。v2 迁移会静默建立一次基线。
2. 在 Actions 列表筛选 `event:schedule`，比较创建时间和各次 Summary。不要用 Telegram 条数或 Git 提交数代替调度统计。
3. 没有运行记录：检查工作流在默认分支、工作流启用状态、仓库 Actions 设置、执行 actor 的账户状态。GitHub 官方也说明：公开仓库 60 天无活动可能禁用定时；本仓库检查时有近期成功记录，不能把“已被禁用”当作已确认根因。
4. 有记录但失败：看 source count、无新闻错误、Secret **名称是否可用**、内容写权限和状态提交步骤；不要把 Secret 值贴到日志或聊天。
5. 若仍是数小时间隔，cron 加密本身不足以满足“尽早”。现有免费方案的补救是手动 Run workflow；可另评估独立免费定时服务调用 `workflow_dispatch`，但需要额外提供者、授权和额度评估，本次没有开通、配置或宣称它可靠。没有新增收费服务。

官方说明：[schedule 事件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。其限制包括高负载时延迟、整点拥堵、足够高负载时任务可能被丢弃，以及仅在默认分支运行。当前仓库的几小时间隔也可能超过普通排队延迟，不能只凭这段一般说明断言具体根因。
