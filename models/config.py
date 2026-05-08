from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class LocalBGEConfig(BaseModel):
    model_id: str = "BAAI/bge-m3"
    device: str = "cuda"
    dim: int = 1024


class LocalQwenConfig(BaseModel):
    model_id: str = "Qwen/Qwen3-Embedding-0.6B"
    device: str = "cuda"
    dim: int = 1024


class SSHConfig(BaseModel):
    host: str
    user: str
    ssh_port: int = 22
    key_file: str | None = None       # 密钥路径，与 password_env 二选一
    password_env: str | None = None   # .env 中存密码的变量名
    remote_port: int = 8000
    local_port: int = 18000


class RemoteConfig(BaseModel):
    base_url: str = "http://localhost:18000"
    ssh: SSHConfig | None = None


class OnlineEmbeddingConfig(BaseModel):
    provider: Literal["voyage", "openai"] = "voyage"
    model: str = "voyage-3-large"
    dim: int = 1024
    api_key_env: str = "VOYAGE_API_KEY"
    base_url: str | None = None   # OpenAI-compatible 第三方服务（ModelScope 等）


class EmbeddingConfig(BaseModel):
    backend: Literal["local_bge", "local_qwen", "remote", "online"] = "local_bge"
    local_bge: LocalBGEConfig = Field(default_factory=LocalBGEConfig)
    local_qwen: LocalQwenConfig = Field(default_factory=LocalQwenConfig)
    remote: RemoteConfig = Field(default_factory=RemoteConfig)
    online: OnlineEmbeddingConfig = Field(default_factory=OnlineEmbeddingConfig)


class LocalRerankerConfig(BaseModel):
    model_id: str = "BAAI/bge-reranker-v2-m3"
    device: str = "cuda"


class OnlineRerankerConfig(BaseModel):
    provider: Literal["cohere", "dashscope"] = "cohere"
    model: str = "rerank-v3.5"
    api_key_env: str = "COHERE_API_KEY"


class RerankerConfig(BaseModel):
    backend: Literal["local", "remote", "online"] = "local"
    local: LocalRerankerConfig = Field(default_factory=LocalRerankerConfig)
    remote: RemoteConfig = Field(default_factory=RemoteConfig)
    online: OnlineRerankerConfig = Field(default_factory=OnlineRerankerConfig)


class DBConfig(BaseModel):
    host: str = "localhost"
    port: int = 5433
    user: str = "macrolens"
    password: str = "macrolens"
    dbname: str = "macrolens"

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.dbname}"


class ChunkingConfig(BaseModel):
    chunk_tokens: int = 512
    chunk_overlap: int = 128


class LLMConfig(BaseModel):
    provider: Literal["anthropic", "gemini"] = "anthropic"
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    temperature: float = 0.0
    top_k: int = 8
    candidate_k: int = 20
    api_key_env: str = "ANTHROPIC_API_KEY"  # 指向 .env 中的变量名


class JudgeLLMConfig(BaseModel):
    provider: Literal["anthropic", "gemini"] = "gemini"
    model: str = "gemini-2.5-pro"
    api_key_env: str = "GEMINI_API_KEY"


class AppConfig(BaseModel):
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    reranker: RerankerConfig = Field(default_factory=RerankerConfig)
    db: DBConfig = Field(default_factory=DBConfig)
    vector_dim: int = 1024
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    judge: JudgeLLMConfig | None = None  # 未配置时 fallback 到 llm


def load_config(path: str = "config.yaml") -> AppConfig:
    import yaml
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw or {})
