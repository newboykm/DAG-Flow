"""审核信任级别（三档）：全部信任 / 部分信任 / 全部不信任。

- all（全部信任）：敏感操作（执行危险命令、运行代码）免人工审批，agent 全自主。
- partial（部分信任，默认）：危险命令、运行代码需审批；普通写文件/编辑/只读免审。
- none（全部不信任）：所有可能改动工作区的操作（写文件/编辑/任何命令/运行代码）都要人工审批。

持久化在 AppConfig（key=trust_level），后端提供 API 读写，卡片上可切换。
"""
from __future__ import annotations

from .db import SessionLocal
from .models import AppConfig

TRUST_LEVELS = ("all", "partial", "none")
DEFAULT_TRUST = "partial"


def get_trust_level(db=None) -> str:
    """读取当前信任档（默认 partial）。"""
    own = db is None
    if own:
        db = SessionLocal()
    try:
        row = db.query(AppConfig).filter(AppConfig.key == "trust_level").first()
        val = row.value if row else ""
        return val if val in TRUST_LEVELS else DEFAULT_TRUST
    finally:
        if own:
            db.close()


def set_trust_level(level: str, db=None) -> str:
    """设置信任档，返回实际生效值。"""
    level = (level or "").strip()
    if level not in TRUST_LEVELS:
        level = DEFAULT_TRUST
    own = db is None
    if own:
        db = SessionLocal()
    try:
        row = db.query(AppConfig).filter(AppConfig.key == "trust_level").first()
        if row:
            row.value = level
        else:
            db.add(AppConfig(key="trust_level", value=level))
        db.commit()
        return level
    finally:
        if own:
            db.close()
