# PactFlow 流程层 V3 实现

本次在现有 Python/LangGraph 运行时接入流程层 V3 的单用户首版。没有切换 Harness，没有激活示例契约，没有运行真实模型或发布服务。产品流程协议与 TaskContract 版本独立，旧契约字段及哈希算法保持兼容。

## 已接入行为

- 契约、资源、工具、安全策略及硬限额共同约束执行。禁止规则优先于人工确认，确认不能解除范围或预算限制。
- 每个 `(run_id, tool_call_id)` 有执行记录，保存参数指纹、策略与来源指纹、授权引用、执行状态和结果。授权消费与预算预留使用 SQLite 同一事务。批次第二次暂停不会重复消费前一动作，终态重放返回原结果。
- 已启动而没有保存结果的动作标记 `outcome_unknown`，不会盲目重试。相同 office 的副作用动作只允许一个执行者；未核对的未知结果阻止后续写执行。
- 本地低风险读写授权可复用已展示的精确资源。没有隐式父目录扩张，没有解释器通配授权；执行、外发、破坏性动作和来源确认不参与范围复用。
- Shell 与受控检查返回结构化状态、退出码和输出。非零退出码判失败，历史失败保留。检查器按契约 `inputs.checks` 中固定的 argv、cwd、超时执行。
- WorkItem、ProductContext、PlanRevision、DecisionRequest 与 Delivery 分别持久化。运行固定契约、工作项及产品上下文修订。普通文件扩展自动记入计划修订，不改变契约哈希。Agent 的产品上下文提案不能替换已确认上下文。
- 节点实例 ID 带 run 前缀，`logical_node_id` 保留业务标识。规划器及入库路径拒绝自依赖、循环和悬空引用。模型声明完成只进入就绪状态，节点必须有 AC 证据才能完成。
- 主图采用 `prepare -> agent/tools/decision -> verify -> agent/finalize`。验证节点自动运行缺失或失效的必要检查，失败可在持久化修复预算内返回 Agent，默认最多三次。多节点计划需要明确 AC 引用，缺少引用时不能自动通过。
- 场景 AC 绑定检查 ID、检查器版本、工作区指纹与工具证据。修改产物会使旧证据失效。旧基础验收规则继续执行，场景通过不能覆盖基础验收失败。
- 技术交付与人工接受分别记录。`acceptance_required=false` 时可自动交付，默认需要人工接受的成果保持 pending。接受或拒绝由可信 CLI 记录，不隐含发布权限。最终报告聚合后原子写入。
- 等待时可查询状态、取消和登记反馈。产品选择使用独立 interrupt；审批继续精确恢复原调用。反馈记录为修订提案，受旧目标影响的动作停止，不把普通文本视作批准。
- 基线记录扫描缺口、截断与链接问题，并保存内容对象。支持带预期哈希的 patch 和内容恢复。可记录 Git HEAD、工作区及暂存区补丁和未跟踪清单，不自动 stash/reset。
- 支持把外部项目导入 office 内副本，并登记 WorkspaceBinding。工具仍限 office，导入不授予宿主原项目写权限。
- 现有 Python 测试删除断言、增加跳过标记或扩大 mock 时触发验收语义确认；其他语言已有测试变化保守要求复核。新测试不因“测试文件”身份而默认暂停。

## 使用与兼容

运行环境仍是 Python 3.10+ 和原有 requirements.txt。离线回归入口自动使用临时 `PACTFLOW_WORKSPACE`，不会调用真实模型。

```powershell
.venv\Scripts\python.exe scripts/run_regression.py
```

示例见 `examples/contracts/v3-scenario.contract.json`，保持 draft。使用前需在 office 的 `products/demo` 准备可信 `check_result.py`，核对解释器、路径及权限，再通过原有 `pactflow contract-approve` 批准。示例禁止 Agent 修改检查脚本。不能把协作对象 JSON 直接写入 TaskContract 顶层。

新增 CLI 命令

```text
pactflow workspace-import <source> <project_id>
pactflow run-status <run_id>
pactflow decisions <run_id>
pactflow resolve-decision <request_id> <choice> --revision 1
pactflow baseline-restore <run_id> <filepath> <expected_hash>
```

`baseline-restore` 是用户直接执行的恢复命令，不暴露给 Agent。恢复已被人修改的文件时必须提供当前内容哈希；不能拿旧哈希强制覆盖。`run-status` 返回 ResumePacket 和独立的人工接受记录。交互 CLI 的 `/status`、`/cancel`、`/pause` 不创建新的 run；产品决策按显示的选项 ID 回复。

数据库升级在 RuntimeStore 初始化时添加表和字段，现存节点重命名并保留 logical_node_id。已有历史被旧版 INSERT OR REPLACE 覆盖的内容无法恢复，迁移元数据明确标记这一限制。旧 LangGraph checkpoint 中的原始节点缓存不能替代数据库的新节点身份；升级前结束或取消旧运行，再创建后继运行。

## 当前边界

这是方案阶段 A/B 的主要闭环以及阶段 C 的基础恢复能力，尚不代表 A-D 所有退出条件完成。

- 当前是应用级限制，宿主进程没有 OS 或网络隔离。固定 argv 和 cwd 无法约束脚本内部访问；运行不可信代码仍需要受控容器。Shell 间接写入不受现有文件数预算完整覆盖。
- 当前仍整图暂停，不实现节点级并发调度、团队身份、跨仓库 write lease 或 Harness 插件部署；这些属于阶段 D。
- 不承诺通用外部副作用 exactly-once。`outcome_unknown` 需要人工调查，当前没有自动判定外部系统结果或解除未知状态的通用恢复入口。
- 反馈提案、授权范围变化及返工后继 run 已保留记录边界，但没有自动把自由文本转成已批准的新契约；需要可信用户入口核对后建立后继运行。
- 测试语义检测是保守检测，不是完整程序语义证明。任意 Shell 改测试、复杂 fixture 数据替换及独立验证执行者适配尚未完成，重要检查脚本应设为 cannot_write。
- 内容哈希与本地锁能检测已发生的人工修改，不构成针对并发外部进程或恶意脚本的 OS 原子写入隔离。完整三方冲突界面尚未实现。
- 持久化计数覆盖工具、受控文件与修复次数；活跃时间、费用预算、软进度计时与产品协作指标尚未全面接入。
- 输出保存在本地账本，已有日志脱敏机制继续存在；跨事件、审批和模型 checkpoint 的统一脱敏与独立大输出存储尚未完成。
- 本轮验证使用真实本地检查进程和模拟模型，不包含真实模型质量评估、网络调用、生产部署或 Harness 集成测试。

## 验证记录

2026-09-05 最终执行 `scripts/run_regression.py`，175 项测试全部通过，用时 19.823 秒；其中原有测试 142 项，新增 V3 测试 33 项。`git diff --check` 通过。

回归覆盖 deny 优先、批量预算、审批重放、未知结果、运行节点隔离、旧数据库迁移、DAG、方案与授权解耦、来源指纹变化、内容冲突、恢复副本、产品决策、人工接受、自动检查和失败返工。CLI 帮助命令及 draft 示例模型校验通过。测试隔离使用临时 PACTFLOW_WORKSPACE，真实检查进程只执行测试定义的本地程序；模型为模拟提供方。
