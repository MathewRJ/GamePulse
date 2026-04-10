"""Configuration loading — reads gamepulse.toml from standard locations.

Environment variable overrides (highest priority):
  ES_URL       → elasticsearch.endpoint
  ES_API_KEY   → elasticsearch.api_key
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ElasticsearchConfig:
    endpoint: str = "http://localhost:9200"
    api_key: str | None = None
    username: str | None = None
    password: str | None = None
    index_prefix: str = "gamepulse"
    flush_interval_secs: int = 5
    batch_size: int = 100


@dataclass
class CollectionConfig:
    interval_ms: int = 1000
    cpu: bool = True
    memory: bool = True
    gpu: bool = True
    storage: bool = True
    network: bool = True
    ebpf: bool = False
    frame_timing: bool = True
    game_detection: bool = True


@dataclass
class PrivacyConfig:
    opt_in_public: bool = False
    share_ebpf: bool = False
    share_network: bool = False


@dataclass
class Config:
    elasticsearch: ElasticsearchConfig = field(default_factory=ElasticsearchConfig)
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)


_SEARCH_PATHS = [
    Path("gamepulse.toml"),
    Path.home() / ".config" / "gamepulse" / "gamepulse.toml",
    Path("/etc/gamepulse/gamepulse.toml"),
]


def load(path: Path | None = None) -> Config:
    """Load config from an explicit path or the first found default location."""
    candidates = [path] if path else _SEARCH_PATHS
    raw: dict = {}
    for candidate in candidates:
        if candidate and candidate.exists():
            with open(candidate, "rb") as f:
                raw = tomllib.load(f)
            break

    cfg = Config()

    if es := raw.get("elasticsearch"):
        cfg.elasticsearch = ElasticsearchConfig(
            endpoint=es.get("endpoint", cfg.elasticsearch.endpoint),
            api_key=es.get("api_key"),
            username=es.get("username"),
            password=es.get("password"),
            index_prefix=es.get("index_prefix", cfg.elasticsearch.index_prefix),
            flush_interval_secs=es.get("flush_interval_secs", cfg.elasticsearch.flush_interval_secs),
            batch_size=es.get("batch_size", cfg.elasticsearch.batch_size),
        )

    if col := raw.get("collection"):
        cfg.collection = CollectionConfig(
            interval_ms=col.get("interval_ms", cfg.collection.interval_ms),
            cpu=col.get("cpu", cfg.collection.cpu),
            memory=col.get("memory", cfg.collection.memory),
            gpu=col.get("gpu", cfg.collection.gpu),
            storage=col.get("storage", cfg.collection.storage),
            network=col.get("network", cfg.collection.network),
            ebpf=col.get("ebpf", cfg.collection.ebpf),
            frame_timing=col.get("frame_timing", cfg.collection.frame_timing),
            game_detection=col.get("game_detection", cfg.collection.game_detection),
        )

    if priv := raw.get("privacy"):
        cfg.privacy = PrivacyConfig(
            opt_in_public=priv.get("opt_in_public", cfg.privacy.opt_in_public),
            share_ebpf=priv.get("share_ebpf", cfg.privacy.share_ebpf),
            share_network=priv.get("share_network", cfg.privacy.share_network),
        )

    # Environment variable overrides — highest priority
    if env_url := os.environ.get("ES_URL"):
        cfg.elasticsearch.endpoint = env_url
    if env_key := os.environ.get("ES_API_KEY"):
        cfg.elasticsearch.api_key = env_key

    return cfg
