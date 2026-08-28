"""模型计费价目表：每 1M token 的单价（元）。

只做内置国产模型参考价；后续可改为用户自定义单价。
"""
from __future__ import annotations

# 元 / 1M token
PRICING_PER_1M: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input": 2.0, "output": 8.0},
    "deepseek-reasoner": {"input": 4.0, "output": 16.0},
    "glm-4-plus": {"input": 10.0, "output": 10.0},
    "glm-4-air": {"input": 1.0, "output": 1.0},
    "glm-4-flash": {"input": 0.0, "output": 0.0},
    "moonshot-v1-8k": {"input": 12.0, "output": 12.0},
    "moonshot-v1-32k": {"input": 24.0, "output": 24.0},
    "kimi-k2": {"input": 12.0, "output": 12.0},
}

DEFAULT_PRICE: dict[str, float] = {"input": 2.0, "output": 8.0}


def price_for(model: str) -> dict[str, float]:
    return PRICING_PER_1M.get(model, DEFAULT_PRICE)


def compute_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按 token 数计价（元）。"""
    p = price_for(model)
    cost = prompt_tokens / 1_000_000 * p["input"] + completion_tokens / 1_000_000 * p["output"]
    return round(cost, 6)
