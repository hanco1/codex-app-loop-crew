# 进阶参考

[README](README.zh-CN.md) 背后更深的机制。大白话版在那边；这里是细节。

## 为什么需要它

这个 skill 有条件地解决普通开发者在多智能体协作中的协调痛点。它是一层控制与审计机制——并不承诺多个 agent 会变得便宜、完全自主，或永远不会停滞。

- **知道 agent 什么时候需要你。** dashboard 会显示 "Ready for you" 提示条，把相关 lane 移到最上方，并指出要打开哪个对话。
- **让项目状态脱离随时可能丢失的聊天记录。** 目标、request、handoff、消息、决策和证据都保存在仓库中，换一个会话也能从文件继续。
- **减少 agent 同时踩到相同文件。** 一个 lane 对应一个持续的 agent 任务，带有明确写入范围；参考工作流要求范围两两不重叠，也能拒绝越界提交。
- **留下可以还原的历史。** 带 lane 标签的 commit、保存的消息 envelope、只追加的状态转换日志，以及逐命令证据，共同记录了什么变化、为什么变化。
- **避免遗留无人接手的脏工作区。** 每个 lane 用一次 commit 结束轮次；暂停时应已全部提交，health check 会暴露范围内残留工作。

这些能力来自[九条方法论不变量](skills/codex-agent-loop-orchestrator/references/methodology.md)，而不是 dashboard UI 本身。

## 核心保证（及其限制）

- **机器检查的完成门槛。** 只有 checker 读到 exit code 成功的证据时才输出 `SHIP_CHECK_OK`。证据缺失、格式错误或非零都按失败处理。这个门槛只验证记录，不会假装自己跑过测试。
- **独立 `review` lane。** 在 product 接受切片前，review 检查未满足的标准、范围膨胀，以及"看起来完成、实际上错"的结果。
- **面向用户工作的人工 QA 门槛。** 先机器检查和 review；随后 request 停在 `REVIEWING`，直到有人操作 UI 并确认。
- **真正可能变红的验收标准。** 每条标准指定一条在要求被破坏时能失败的命令。对垃圾输出仍保持绿色的检查不算证据。
- **不变量优先的 intake。** 数据和多步骤系统先在 `goal.md` 记录绝不能破坏的规则，再把适用不变量带入每个 request。
- **有边界的恢复机制。** Heartbeat、停滞 handoff 检查、明确的修复轮次上限和持久预算提供恢复路径——但不声称能自动唤醒已停止的对话。
- **Runtime tier 指引。** 每个 lane 记录一个抽象 model tier，默认用 host 最高 tier，并暴露实际 tier 不匹配；人可以主动调低某个 lane。

实现和限制见 skill 的 [Health Check](skills/codex-agent-loop-orchestrator/SKILL.md#health-check)、[Verification Integrity](skills/codex-agent-loop-orchestrator/SKILL.md#verification-integrity) 和 [Model Tier Policy](skills/codex-agent-loop-orchestrator/SKILL.md#model-tier-policy)。

## Lane 与归属

默认团队是 `product`、一个 build lane 和 `review`。只有当 `data-eng`、`frontend`、`security` 或其他 specialist 拥有一个持续职责、有清晰的输入输出路由和不重叠写入范围时，才添加它。Lane 是专业分工，不是人格或产品功能。

`product` 负责 `docs/loop/**` 下的 loop ledger。build lane 各管独立的代码和测试子树。每个 lane 还负责自己的 `docs/loop/lanes/<lane>/**` worklog 区域。

## Request 生命周期

`requests.md` 是队列和恢复索引。一个 blocker 修复周期内复用同一个 `request_id`，同时递增 `iteration`：

```text
PLANNED -> REQUESTED -> IMPLEMENTING -> IMPLEMENTATION_DONE -> REVIEWING
REVIEWING -> FIX_REQUESTED | ACCEPTED | BLOCKED
FIX_REQUESTED -> IMPLEMENTING
BLOCKED -> FIX_REQUESTED | ABANDONED
```

跨对话投递前，typed message 先保存在 `docs/loop/messages/<request_id>/`。若无法投递到 thread，原子化文件 inbox 可保住消息——但文件 inbox 不是自动 worker。

## 由机器检查完成状态

implementation lane 运行每条验收命令，写一条扁平证据记录，含 request、checkpoint、command、exit code 和 timestamp。`completion_gate.py` 读这些记录。无法验证就是 `BLOCKED`，绝不"带保留意见接受"。

## 面向用户的工作等待人工 QA

机器证据和 review 通过后，UI request 仍停在 `REVIEWING`。product 发一个 URL 和简短试用说明；只有明确的 `human_qa: confirmed` 记录才解锁 `ACCEPTED`。

## dashboard

dashboard 是仓库文件和只读 health check 之上的本地查看器。它显示 Progress、当前人工门槛、lane 归属、request、证据、Git/hook 健康状况、用量可用性和运行日志。在提示条指出去哪操作之前，人始终待在 product 对话里。

## Git 模型

参考工作流使用**一条共享分支加线性、带 lane 标签的 commit 历史**——这是约定，不是脚本强制的分支限制。

- **每轮以 lane 身份提交。** lane 完成切片、更新 worklog 和持久 request 状态，然后在回复或 handoff 前提交。
- **武装 scope guard。** `install_precommit.py` 安装一个 Git pre-commit 检查。启用后，缺 `CODEX_LANE` 按失败处理，暂存了该 lane 声明范围之外的文件会被拒绝。
- **保持写入范围不重叠。** 静态 lane 范围必须两两不重叠；动态文件 lease 覆盖有界的例外。guard 在提交时起作用，因此无法阻止两个进程在提交前编辑同一文件。
- **只从干净的检查点暂停。** 暂停的 loop 应已全部提交。product 检查 `git status --porcelain`，health check 报告可归属的范围内残留。
- **私有 remote 只做备份。** 它可保存检查点 commit 用于灾难恢复；它不是 lane 消息总线，敏感或原始数据绝不能仅因 remote 是私有的就被提交。

```bash
CODEX_LANE=frontend git commit -m "frontend: finish request REQ-004"
```

```powershell
$env:CODEX_LANE = 'frontend'
git commit -m 'frontend: finish request REQ-004'
```

### 为什么不给每个 lane 一个 Git worktree？

这个参考实现依赖所有 lane 立即看到同一份 request ledger、证据和转换日志。它在范围冲突时刻意串行化写入，且不实现分支创建、合并、rebase 或跨 worktree 的状态协调。给每个 lane 一个 worktree 会引入第二套协调系统，可能让某个 lane 基于过期 ledger 行动。如果你选择 worktree，那是在设计另一种实现，需要一套显式的合并/协调协议；参考 scope guard 不提供这个。

## 日常使用

待在长期的 **product 对话**里，并保持 dashboard 打开。product 是新工作、验收变更和最终产品判断的持久入口。

对于 UI 改动，在**同一个对话**里请 product：

```text
请收紧 dashboard 头部间距并优化主按钮层级。通过现有的 frontend lane 走正常的 review + 人工 QA 门槛。
```

不要为每个改动开临时对话，也不要让 build lane 绕过 product。直接的 lane 请求会被送回正常 request 生命周期；它绝不是绕过证据或 review 的捷径。只有当注册的对话确实过期或缺失时，才创建替代 lane 对话，然后把它接入现有 lane 行。

## 仓库结构

```text
codex-app-loop-crew/
├── .agents/plugins/marketplace.json
├── .codex-plugin/plugin.json
├── assets/
├── skills/codex-agent-loop-orchestrator/
│   ├── SKILL.md
│   ├── agents/
│   ├── references/
│   └── scripts/
├── install.ps1
├── install.sh
├── COMPARISON.md
├── ADVANCED.md
├── ADVANCED.zh-CN.md
├── README.zh-CN.md
├── README.md
└── LICENSE
```
