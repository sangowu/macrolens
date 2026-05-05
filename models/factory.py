from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from models.base import EmbeddingBackend, RerankerBackend
from models.config import AppConfig
from models.llm.base import LLMClient


def create_embedding(cfg: AppConfig) -> EmbeddingBackend:
    backend = cfg.embedding.backend

    if backend == "local_bge":
        from models.embedding.local_bge import LocalBGEEmbedding
        c = cfg.embedding.local_bge
        return LocalBGEEmbedding(model_id=c.model_id, device=c.device)

    if backend == "local_qwen":
        from models.embedding.local_qwen import LocalQwenEmbedding
        c = cfg.embedding.local_qwen
        return LocalQwenEmbedding(model_id=c.model_id, device=c.device)

    if backend == "remote":
        from models.embedding.remote import RemoteEmbedding
        return RemoteEmbedding(base_url=cfg.embedding.remote.base_url)

    if backend == "online":
        from models.embedding.online import OnlineEmbedding
        c = cfg.embedding.online
        return OnlineEmbedding(
            provider=c.provider,
            model=c.model,
            dim=c.dim,
            api_key=os.environ[c.api_key_env],
            base_url=c.base_url,
        )

    raise ValueError(f"Unknown embedding backend: {backend!r}")


def create_reranker(cfg: AppConfig) -> RerankerBackend:
    backend = cfg.reranker.backend

    if backend == "local":
        from models.reranker.local import LocalReranker
        c = cfg.reranker.local
        return LocalReranker(model_id=c.model_id, device=c.device)

    if backend == "remote":
        from models.reranker.remote import RemoteReranker
        return RemoteReranker(base_url=cfg.reranker.remote.base_url)

    if backend == "online":
        from models.reranker.online import OnlineReranker
        c = cfg.reranker.online
        return OnlineReranker(
            provider=c.provider,
            model=c.model,
            api_key=os.environ.get(c.api_key_env, ""),
        )

    raise ValueError(f"Unknown reranker backend: {backend!r}")


def create_llm_client(cfg: AppConfig) -> LLMClient:
    api_key = os.environ[cfg.llm.api_key_env]

    if cfg.llm.provider == "anthropic":
        from models.llm.anthropic_client import AnthropicClient
        return AnthropicClient(model=cfg.llm.model, api_key=api_key)

    if cfg.llm.provider == "gemini":
        from models.llm.gemini_client import GeminiClient
        return GeminiClient(model=cfg.llm.model, api_key=api_key)

    raise ValueError(f"Unknown LLM provider: {cfg.llm.provider!r}")


@contextmanager
def managed_models(
    cfg: AppConfig,
) -> Generator[tuple[EmbeddingBackend, RerankerBackend], None, None]:
    """启动所需 SSH tunnel，yield (embedding, reranker)，退出时自动清理。"""
    tunnels: list = []
    try:
        _start_tunnels_if_needed(cfg, tunnels)
        embedding = create_embedding(cfg)
        reranker = create_reranker(cfg)
        yield embedding, reranker
    finally:
        for t in tunnels:
            t.stop()


def _start_tunnels_if_needed(cfg: AppConfig, tunnels: list) -> None:
    """为所有 remote backend 启动 SSH tunnel，同一 local_port 只建一条。"""
    try:
        from sshtunnel import SSHTunnelForwarder
    except ImportError:
        return

    seen: set[int] = set()
    candidates = []
    if cfg.embedding.backend == "remote":
        candidates.append(cfg.embedding.remote)
    if cfg.reranker.backend == "remote":
        candidates.append(cfg.reranker.remote)

    for remote_cfg in candidates:
        if remote_cfg.ssh is None:
            continue
        ssh = remote_cfg.ssh
        if ssh.local_port in seen:
            continue
        seen.add(ssh.local_port)
        auth = {}
        if ssh.password_env:
            auth["ssh_password"] = os.environ[ssh.password_env]
        elif ssh.key_file:
            auth["ssh_pkey"] = os.path.expanduser(ssh.key_file)

        tunnel = SSHTunnelForwarder(
            ssh_address_or_host=(ssh.host, ssh.ssh_port),
            ssh_username=ssh.user,
            remote_bind_address=("127.0.0.1", ssh.remote_port),
            local_bind_address=("127.0.0.1", ssh.local_port),
            **auth,
        )
        tunnel.start()
        tunnels.append(tunnel)
