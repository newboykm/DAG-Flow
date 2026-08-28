"""Pydantic 请求/响应 schema（对照需求 §3.2 / §6）。"""
from typing import Literal, Optional
from pydantic import BaseModel, Field

NodeStatus = Literal[
    "pending", "ready", "running", "done", "failed", "cancelled", "paused", "blocked"
]
AppendMode = Literal["serial", "parallel", "join"]


class NodeOut(BaseModel):
    nodeId: str
    sessionId: str
    parentIds: list[str] = []
    title: str
    status: NodeStatus
    kind: str
    input: dict
    messages: list[dict] = []
    output: Optional[dict] = None
    progress: Optional[dict] = None
    contextRef: Optional[str] = None
    parentContext: list[dict] = []
    plan: Optional[dict] = None
    collapsed: Optional[bool] = None
    subtreeCollapsed: bool = False
    dragOffset: Optional[dict] = None
    customSize: Optional[dict] = None
    meta: dict = {}
    model: Optional[str] = None
    files: list[dict] = []
    createdAt: str = ""
    updatedAt: str = ""


class EdgeOut(BaseModel):
    id: str
    source: str
    target: str


class GraphOut(BaseModel):
    sessionId: str
    nodes: list[NodeOut]
    edges: list[EdgeOut]


class AddNodeRequest(BaseModel):
    mode: AppendMode
    anchorNodeId: Optional[str] = None
    parentIds: Optional[list[str]] = None
    input: dict = Field(default_factory=dict)


class UpdateNodeRequest(BaseModel):
    title: Optional[str] = None
    input: Optional[dict] = None
    collapsed: Optional[bool] = None
    subtreeCollapsed: Optional[bool] = None
    dragOffset: Optional[dict] = None
    customSize: Optional[dict] = None
    model: Optional[str] = None


class ResolveBlockedRequest(BaseModel):
    action: Literal["skip", "cancel"]


class SendMessageRequest(BaseModel):
    text: str


class ModelConfigIn(BaseModel):
    provider: Optional[str] = None
    baseUrl: Optional[str] = None
    apiKey: Optional[str] = None
    model: Optional[str] = None


class ModelConfigOut(BaseModel):
    hasConfig: bool
    provider: str = ""
    baseUrl: str = ""
    apiKey: str = ""
    model: str = ""


class CreateSessionRequest(BaseModel):
    title: Optional[str] = None
    workspace: Optional[str] = None


class SessionOut(BaseModel):
    sessionId: str
    title: Optional[str] = None
    workspace: Optional[str] = None
    createdAt: str = ""
    updatedAt: str = ""
