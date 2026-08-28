"""ORM 数据模型（对照需求 §3 数据结构）。

边不单独建表，用 Node.parentIds（JSON 存储）表达拓扑（§3.3）。
"""
from sqlalchemy import String, Integer, JSON, Float, Boolean, ForeignKey, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime, timezone

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Session(Base):
    __tablename__ = "sessions"

    sessionId: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    workspace: Mapped[str | None] = mapped_column(String, nullable=True)
    contextVersion: Mapped[int] = mapped_column(Integer, default=0)
    # 用量统计：{promptTokens, completionTokens, cost}
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 用户设定的预算（元）；余额 = budget - cost
    budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updatedAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    nodes: Mapped[list["Node"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    contextEntries: Mapped[list["ContextEntry"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Approval(Base):
    """工具调用人工审批请求（human-in-the-loop）。"""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    sessionId: Mapped[str] = mapped_column(String, ForeignKey("sessions.sessionId"), index=True)
    nodeId: Mapped[str] = mapped_column(String, index=True)
    tool: Mapped[str] = mapped_column(String)
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending | approved | rejected
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updatedAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    session: Mapped["Session"] = relationship()


class ContextEntry(Base):
    """共享上下文条目：key -> value + 版本 + 发布节点（§4.2.2/4.2.3）。"""

    __tablename__ = "context_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sessionId: Mapped[str] = mapped_column(String, ForeignKey("sessions.sessionId"), index=True)
    key: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    sourceNodeId: Mapped[str | None] = mapped_column(String, nullable=True)
    # 冲突态：active=正常；conflict=待用户裁决
    status: Mapped[str] = mapped_column(String, default="active")
    # 冲突候选：[{value, sourceNodeId}]，裁决时二选一或合并
    candidates: Mapped[list[dict]] = mapped_column(JSON, default=list)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updatedAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    session: Mapped["Session"] = relationship(back_populates="contextEntries")


class ContextBlock(Base):
    """节点每轮执行的「内容块」：不可变，按时间追加，供下游继承完整历史（按需取全文）。"""

    __tablename__ = "context_blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sessionId: Mapped[str] = mapped_column(String, ForeignKey("sessions.sessionId"), index=True)
    nodeId: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, default="")
    seq: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(Text, default="")
    fulltext: Mapped[str] = mapped_column(Text, default="")
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    session: Mapped["Session"] = relationship()


class AppConfig(Base):
    """全局配置键值对（如 skill 目录路径）。"""

    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class McpServer(Base):
    """MCP 服务器配置（stdio 模式）：command + args，供 agent 动态接入第三方工具。"""

    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    command: Mapped[str] = mapped_column(String)
    args: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Node(Base):
    __tablename__ = "nodes"

    nodeId: Mapped[str] = mapped_column(String, primary_key=True)
    sessionId: Mapped[str] = mapped_column(String, ForeignKey("sessions.sessionId"), index=True)
    parentIds: Mapped[list[str]] = mapped_column(JSON, default=list)
    title: Mapped[str] = mapped_column(String, default="新任务")
    status: Mapped[str] = mapped_column(String, default="pending")
    kind: Mapped[str] = mapped_column(String, default="task")  # root | task
    inputText: Mapped[str] = mapped_column(Text, default="")
    messages: Mapped[list[dict]] = mapped_column(JSON, default=list)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    contextRef: Mapped[str | None] = mapped_column(String, nullable=True)
    collapsed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    subtreeCollapsed: Mapped[bool] = mapped_column(Boolean, default=False)
    dragOffset: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    customSize: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    mode: Mapped[str | None] = mapped_column(String, nullable=True)
    retryOf: Mapped[str | None] = mapped_column(String, nullable=True)
    failedReason: Mapped[str | None] = mapped_column(String, nullable=True)
    # 卡片级模型选择（如 deepseek-chat）；空则用服务商默认
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    # 上传到 workspace 并与卡片关联的文件列表 [{path, name}]
    files: Mapped[list[dict]] = mapped_column(JSON, default=list)
    skippedParents: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 节点读上下文基线：节点启动时每个 key 的版本快照 {key: version}
    baseContext: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 下游节点的父上下文索引：{parentNodeId: [{seq, summary, title}...]}，父节点发布新内容块时自动刷新
    parentContext: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 卡片级滚动记忆：{summary: str, key_facts: [str], recent_actions: [str]}（每次完成 LLM 折叠）
    memory: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 任务计划：{goal: str, steps: [{label, status}]}（status: pending|running|done|failed）
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updatedAt: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    session: Mapped["Session"] = relationship(back_populates="nodes")
