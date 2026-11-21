# 架构

## 设计取舍

- **技能 = SKILL.md 指南 + 脚本 + 参数 schema + verify + 回归用例 + 版本历史**，全部是人类可读的文件，可以直接手改
- **步骤是修补的最小单位**：失败时只把出错的步骤、错误、状态、接管过程交给强模型，只重写那一个函数——改动小，也容易审查
- **混合脚本**：确定的部分写成代码，需要理解的部分用 `ctx.llm()` 调小模型
- **录制回放让测试几乎零成本**：前提是工具正确标注 `side_effect` / `stateful`
- **门禁在后台**：回归测试、影子运行都不增加用户的等待时间
- **共享即代码**：技能走 git，按签名决定信任

## 代码结构

| 路径 | 作用 |
|---|---|
| [agent.py](../src/no_agent/agent.py) | 对外入口 `Agent`：装配、生命周期、`run` / `run_sync` / `feedback` |
| [config.py](../src/no_agent/config.py) | `AgentConfig` / `LLMConfig` / `BrowserConfig` 等全部配置 |
| [engine/agent.py](../src/no_agent/engine/agent.py) | 主流程：匹配 → 执行 → 接管 → 记录 → 投递进化任务 |
| [engine/freeform.py](../src/no_agent/engine/freeform.py) | 自由执行 / 接管（工具调用循环） |
| [engine/evolver.py](../src/no_agent/engine/evolver.py) | 后台进化器：修补、提炼、升级、影子运行 |
| [engine/testing.py](../src/no_agent/engine/testing.py) | 上线门禁：录制回放测试 |
| [skills/matcher.py](../src/no_agent/skills/matcher.py) | 本地召回（向量 / 词法）+ 小模型判定与参数抽取 |
| [skills/runtime.py](../src/no_agent/skills/runtime.py) | 逐阶段编排（guard → 步骤 → verify），精确报告失败步骤、错误、当时状态 |
| [skills/runner.py](../src/no_agent/skills/runner.py)、[process.py](../src/no_agent/skills/process.py)、[_worker.py](../src/no_agent/skills/_worker.py)、[_confine.py](../src/no_agent/skills/_confine.py) | 脚本在哪里执行：进程内 / 子进程 / Linux 内核约束 |
| [skills/sandbox.py](../src/no_agent/skills/sandbox.py)、[script.py](../src/no_agent/skills/script.py) | 脚本静态校验、受限加载、按函数修补、`SCRIPT_API_DOC` |
| [skills/compose.py](../src/no_agent/skills/compose.py) | 技能组合：`ctx.skill()` 调用子技能 |
| [skills/review.py](../src/no_agent/skills/review.py) | 变更审查：新版本是否扩大了能力 |
| [skills/signing.py](../src/no_agent/skills/signing.py) | 技能签名与验证（Ed25519） |
| [skills/skillmd.py](../src/no_agent/skills/skillmd.py) | SKILL.md 解析、校验、渲染 |
| [storage/](../src/no_agent/storage/) | 存储协议、分层文件技能库、SQL 运行记录、home 解析 |
| [tools/](../src/no_agent/tools/) | 工具层（确认 → 权限 → 执行 → 录制；回放）；内置工具；浏览器工具 |
| [interact.py](../src/no_agent/interact.py) | 提问与确认：`Asker` 协议、终端实现、`ask_user` 工具 |
| [builtin_skills/](../src/no_agent/builtin_skills/) | 随框架分发的只读技能（`fill-form`） |
| [server/](../src/no_agent/server/) | 可选 HTTP 服务 |
| [integrations/](../src/no_agent/integrations/) | MCP 服务端 / 客户端、browser-use |
| [cli.py](../src/no_agent/cli.py) | 命令行 `No-Agent` |

## 已知限制 / 后续方向

- **隔离档位**：默认的 `process` 档不阻止脚本（逃逸 Python 层沙箱后）读写本机文件或直接联网；强隔离的 `sandbox` 档目前只支持 Linux
- **单进程**：进化器是进程内 asyncio 队列，文件技能库用进程内锁。多进程 / 多实例部署需要外部队列和带锁的存储（可通过自定义 `SkillStore` 实现）
- **命名空间（多租户）**：尚未支持，计划后续加入，届时通过 `schema_version` 迁移已有数据
- **共享技能的上游更新**：技能一旦复制到本地（被修补 / 转正），本地版本优先，不会自动合并上游更新
- **原子技能**：随父技能一起提炼时直接上线（只能被调用，不参与匹配）；父技能在影子期被淘汰时，原子技能会保留
- **浏览器**：每个 Agent 共用一个浏览器上下文，暂不支持文件上传、验证码；`run_sync` 每次调用结束都会关闭浏览器
- **browser_agent 的轨迹是黑盒**：经过它的部分无法被提炼成确定性脚本
- **MCP 服务**：目前只提供 stdio 传输

## 路线图（方案二：分层编译）

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | SKILL.md 格式适配、指南型技能、安装、显式调用 | ✅ 完成 |
| 2 | 前置检查（guard）、影子运行后自动转正、权限强制检查 | ✅ 完成 |
| 3 | MCP 服务端 / 客户端、接入 browser-use 作为 AI 浏览层、命令行 | ✅ 完成 |
| 4 | 原子技能与技能组合、按热度升级（指南 → 脚本）、多候选提炼、git 技能仓库 + 签名 | ✅ 完成 |
