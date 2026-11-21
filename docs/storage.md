# 数据存储

框架不替你决定数据放哪，但默认值是安全的：

```
<你的项目>/
  .no-agent/
    skills/        学到的技能 —— 建议提交到 git（可审查、可回滚、团队共享）
    runs.db        运行轨迹   —— 不提交
    workspace/     工具工作区 —— 不提交
    .stats/        技能运行统计 —— 不提交
    .gitignore     自动生成，已按上面的规则配置
```

## `.no-agent/` 的位置

按以下顺序确定（和 git 找 `.git/` 一样，在子目录启动不会另起一份数据）：

1. `AgentConfig(home=...)`
2. 环境变量 `NO_AGENT_HOME`
3. 从当前目录向上查找已有的 `.no-agent/`
4. 在当前目录创建

## 单独覆盖

| 配置 | 默认 | 说明 |
|---|---|---|
| `skills_dir` | `<home>/skills` | 可写技能层，进化只写这里 |
| `extra_skill_dirs` | `[]` | 只读技能层（团队共享库），同 id 时本地覆盖共享 |
| `database_url` | `sqlite:///<home>/runs.db` | 任意 SQLAlchemy URL；表名带 `no_agent_` 前缀，可直接放进已有数据库 |
| `workspace_dir` | `<home>/workspace` | 文件工具的沙箱目录 |

## 分层与写时复制

技能层的优先级从低到高：框架内置 → git 技能仓库 / `extra_skill_dirs` → 本地可写层。只读层的技能被修补、切换版本或下线时，会先整体复制到可写层再修改，共享库本身不会被改动；只是"用了一下"只记统计，不会触发复制。

技能一旦复制到本地，本地版本优先，不会自动合并上游的更新。

## 自定义存储

实现 `SkillStore` / `RunStore` 协议（[storage/base.py](../src/no_agent/storage/base.py)），传给 `Agent(skill_store=..., run_store=...)`。例如技能存 S3、运行记录写入已有的数据仓库。

## 结构版本

技能目录（当前 v2）和运行记录库（当前 v1）各自记录 `schema_version`，分别迁移：技能旧格式在加载时自动改写，数据库迁移登记在 [storage/sql.py](../src/no_agent/storage/sql.py) 的 `MIGRATIONS` 中、启动时执行。遇到比框架更新的数据会拒绝加载，避免旧版本写坏新数据。
