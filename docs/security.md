# 安全模型

AI 会自己写脚本、改脚本、从共享库加载别人的脚本，所以框架从几层限制脚本能做什么：

| 层 | 管什么 | 见 |
|---|---|---|
| 脚本静态校验 | 只允许纯计算的 import，禁止 `open` / `eval` / `getattr` / 私有属性等 | [skills/sandbox.py](../src/no_agent/skills/sandbox.py) |
| 进程隔离 | 脚本在一次性子进程中执行，拿不到密钥和宿主对象 | [脚本隔离](#脚本隔离) |
| 权限声明 | 脚本只能用声明过的工具、访问声明过的域名 | [权限](#权限) |
| 强制确认 | 提交、付款等操作由框架拦截确认，不依赖脚本或模型自觉 | [浏览器与人机交互](browser.md#提问与确认) |
| 变更审查 | AI 产出的新版本扩大了能力时等人批准 | [变更审查](#自动上线前的变更审查) |
| 签名与信任 | 共享技能按签名决定是否执行 | [共享技能](#共享技能签名与信任) |
| HTTP 鉴权 | 对外服务需要 token | [服务与命令行](server.md#鉴权) |

## 脚本隔离

技能脚本默认在一次性子进程中执行（`script_isolation="process"`）。工具、LLM、子技能调用和权限检查都留在主进程，子进程只能通过 JSON 消息请主进程代办，环境变量里也没有 API Key。

| 配置 | 默认 | 说明 |
|---|---|---|
| `script_isolation` | `"process"` | `"sandbox"`：在 `process` 之上再加内核约束（仅 Linux，需 5.13+ 且启用 Landlock），只读文件系统，禁止联网、执行程序、创建进程；内核不支持时拒绝启动。`"inprocess"`：在本进程内执行，只有 Python 层的白名单，仅用于测试或完全信任所有脚本的场景 |
| `worker_pool_size` | `2` | 预热的空子进程数 |
| `worker_memory_mb` | `512` | 每个子进程的内存上限。Linux 用 `RLIMIT_AS`、Windows 用 Job Object，超限的分配直接失败；macOS 不支持 `RLIMIT_AS`，改为按峰值常驻内存检查：超限后终止这次执行（报 `MemoryError`），但拦不住单次超大的分配本身 |
| `worker_cpu_seconds` | `300` | CPU 时间兜底上限，主进程崩溃时也能终止失控的脚本 |
| `script_max_tool_calls` / `script_max_llm_calls` | `500` / `100` | 一次执行（含子技能）的调用次数上限 |

`process` 档能保证：脚本即使逃逸了 Python 层的沙箱，也拿不到密钥、绕不过工具和域名权限；死循环会在步骤超时后被终止。它**不**阻止脚本读写当前用户能访问的文件，也不阻止直接联网。需要这一级隔离时，请在 Linux 上使用 `"sandbox"`；Windows / macOS 上执行不可信脚本，请部署到 Linux 容器中。

实现：[skills/process.py](../src/no_agent/skills/process.py)（子进程与预热池；只用 spawn 不用 fork，只用 JSON 不用 pickle；Windows 上用 Job Object 限制资源）、[skills/_worker.py](../src/no_agent/skills/_worker.py)（worker 协议）、[skills/_confine.py](../src/no_agent/skills/_confine.py)（Linux Landlock + seccomp）。

## 权限

在 `no-agent.json` 中声明 `permissions.tools` / `domains` / `skills` 后，脚本运行时只能调用声明过的工具、访问声明过的域名、调用声明过的子技能（AI 接管不受限）。

- 修补不能扩大权限：用了未声明工具的补丁会被打回；安装时也做同样的静态检查
- 新提炼的技能按实际使用自动生成权限声明
- `guard` 中只能调用只读工具，不能调用子技能
- 子技能在自己声明的权限下运行；子技能没有声明的工具 / 域名不会因此放开，而是继承调用方的限制

## 自动上线前的变更审查

AI 修补或提炼出的新版本，只要**扩大了能力**，就不会自动上线，而是等人批准（[skills/review.py](../src/no_agent/skills/review.py)）：

- 用了新的工具：以技能声明的工具为准；没有声明时，以当前版本脚本实际用到的工具为准。新技能则以轨迹里真实用过的工具为准
- 调用了新的子技能
- 新增了动态调用：`ctx.tool` / `ctx.skill` 的目标不是字面量，静态分析看不到会调用什么。按调用目标的表达式与基准版本比较（不看行号），换了目标或多出一处都会触发

只改逻辑的修补照常自动上线。新增 import、`ctx.llm` 调用点的变化会记录在审查报告里，但不触发审查。

```bash
No-Agent reviews --diff                          # 查看待审版本：原因 + 与基准版本的 diff
No-Agent approve page-title 2 --comment "看过了"  # 批准：之后照常进入影子运行或上线
No-Agent reject page-title 2 --reason "不该发邮件"
```

HTTP：`GET /api/v1/reviews`，`POST /api/v1/skills/{id}/versions/{v}/approve`（或 `/reject`）。

## 共享技能：签名与信任

```python
AgentConfig(
    skill_repos=[SkillRepoConfig(url="https://github.com/your-team/skills.git", subdir="skills")],
    trusted_signers={"team": "<公钥>"},
)
await agent.sync_skill_repos()      # 或命令行 No-Agent sync；启动时不联网，只加载已同步的副本
```

```bash
No-Agent keygen --out team.key                         # 生成密钥（私钥妥善保管），打印公钥
No-Agent sign skills/fill-form --key team.key --signer team
```

共享层（`skill_repos` / `extra_skill_dirs`）里的技能按签名决定信任（需要 `pip install "No-Agent[sign]"`）：

| 情况 | 处理 |
|---|---|
| 框架内置 / 本项目自己的技能 | 直接使用 |
| 由 `trusted_signers` 中的签名者签名，且签名后未被修改 | 直接使用 |
| 未签名 / 签名者不可信 / 签名后被改过 | **隔离**：不执行、不参与匹配、不能被其他技能 `ctx.skill()` 调用，显式 `agent.skill(id)` 也会拒绝；签名或 `install` 显式信任后才可用 |

签名（Ed25519）覆盖技能目录下每个文件的 SHA-256，任何改动都会让签名失效。

`untrusted_shared` 调整对不可信共享技能的处理：

- `"block"`（默认）：如上，完全不执行
- `"shadow"`：流程型先影子运行，在你自己的任务上与真实结果比对，通过后转正（复制到本地）；指南型仍不自动使用。不可信脚本的影子运行只在 `script_isolation="sandbox"`（Linux）下执行，其他档位会跳过
- `"allow"`：关闭这套检查，全部信任
