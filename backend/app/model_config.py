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
    models: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # 敏感字段「透明加密」：DB 列名仍是 apiKey，但读写自动加解密，调用方拿到的都是明文
    _apiKey: Mapped[str] = mapped_column("apiKey", Text, default="")

    @property
    def apiKey(self) -> str:
        """主 key（第一个），保持旧调用兼容。多 key 时请用 api_keys。"""
        return self.api_keys[0] if self.api_keys else ""

    @apiKey.setter
    def apiKey(self, value: str) -> None:
        from .secure import encrypt
        self._apiKey = encrypt(value or "")

    @property
    def api_keys(self) -> list[str]:
        """拆分后的全部 key：多个 key 用换行 / 逗号 / 分号 / 「|||」分隔。"""
        from .secure import decrypt
        raw = (decrypt(self._apiKey) or "").strip()
        if not raw:
            return []
        import re as _re
        parts = [p.strip() for p in _re.split(r"\n|,|;|\|\|\|", raw) if p.strip()]
        return parts

    # 轮询游标（进程级）：多 provider 各自独立计数，避免同时打同一 key
    _key_cursor: dict[str, int] = {}

    def pick_api_key(self) -> str:
        """轮询返回一把 key；同一 provider 的多 key 均摊负载。"""
        keys = self.api_keys
        if not keys:
            return ""
        c = ModelProvider._key_cursor.get(self.id, 0)
        ModelProvider._key_cursor[self.id] = c + 1
        return keys[c % len(keys)]

    @property
    def api_key_full(self) -> str:
        """完整的明文 key 串（含多 key 分隔符），供前端编辑往返。"""
        from .secure import decrypt
        return decrypt(self._apiKey) or ""


# 旧单配置表保留兼容
class ModelConfig(Base):
    __tablename__ = "model_config"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="default")
    provider: Mapped[str] = mapped_column(String, default="")
    baseUrl: Mapped[str] = mapped_column(String, default="")
    model: Mapped[str] = mapped_column(String, default="")

    # 同透明加解密
    _apiKey: Mapped[str] = mapped_column("apiKey", Text, default="")

    @property
    def apiKey(self) -> str:
        from .secure import decrypt
        return decrypt(self._apiKey)

    @apiKey.setter
    def apiKey(self, value: str) -> None:
        from .secure import encrypt
        self._apiKey = encrypt(value or "")


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
