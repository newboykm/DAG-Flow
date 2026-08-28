"""模型配置存储：多个服务商（OpenAI 兼容），每个含 provider/base_url/api_key/模型列表。

持久化到 SQLite。初始化时预置国产模型预设（api_key 留空待用户填）。
"""
from sqlalchemy import String, Text, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


# 国产模型预设（base_url 为 OpenAI 兼容端点）
PROVIDER_PRESETS = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-air", "glm-4-flash"],
    },
    "kimi": {
        "label": "Kimi (Moonshot)",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "kimi-k2"],
    },
}


class ModelProvider(Base):
    __tablename__ = "model_providers"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # provider key
    label: Mapped[str] = mapped_column(String, default="")
    baseUrl: Mapped[str] = mapped_column(String, default="")
    apiKey: Mapped[str] = mapped_column(Text, default="")
    models: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


# 旧单配置表保留兼容
class ModelConfig(Base):
    __tablename__ = "model_config"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="default")
    provider: Mapped[str] = mapped_column(String, default="")
    baseUrl: Mapped[str] = mapped_column(String, default="")
    apiKey: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String, default="")


def seed_providers(db) -> None:
    """确保预设服务商存在（不覆盖用户已填的配置）。"""
    from .db import SessionLocal  # noqa: F401
    existing = {p.id: p for p in db.query(ModelProvider).all()}
    for key, preset in PROVIDER_PRESETS.items():
        if key in existing:
            continue
        db.add(
            ModelProvider(
                id=key,
                label=preset["label"],
                baseUrl=preset["base_url"],
                apiKey="",
                models=preset["models"],
                enabled=False,
            )
        )
    db.commit()


def available_models(db) -> list[dict]:
    """返回所有「已填 key」的服务商及其可用模型（供卡片选择）。"""
    result = []
    for p in db.query(ModelProvider).all():
        if p.apiKey and p.baseUrl:
            for m in p.models:
                result.append({"provider": p.id, "label": p.label, "model": m})
    return result
