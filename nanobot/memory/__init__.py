"""持久化记忆基础包。

Phase 0 只在这里建立 memory 子系统的文件边界；运行时行为由后续任务逐项加入。

事实源边界保持明确：

* Audit 的 JSONL 与 SQLite 索引继续归 `nanobot.audit` 管理；
* workspace 记忆数据库及其迁移、锁和 outbox 归本包管理；
* Markdown、Wiki 页面和 ``SKILL.md`` 仍是权威事实源，SQLite 只保存受控缓存和索引。

本包当前不导出数据库连接、迁移或 Agent 集成 API。
"""

__all__: list[str] = []
