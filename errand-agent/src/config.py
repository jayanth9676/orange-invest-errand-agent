"""Configuration with safe local .env loading and no secret logging."""

import os
from dataclasses import dataclass, field
from pathlib import Path


from typing import Optional


def _load_project_env() -> None:
    """Load the repository .env once, while letting deployed env vars win."""
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",
        Path(__file__).resolve().parent.parent.parent / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in candidates:
        if env_path.is_file():
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key and key.replace("_", "").isalnum():
                    os.environ.setdefault(key, value.strip().strip('"').strip("'"))
            break


_load_project_env()


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    val = os.getenv(name, default)
    if not val or not val.strip():
        val = default
    return tuple(item.strip() for item in val.split(",") if item.strip())


@dataclass
class AgentConfig:
    # auto selects PRIMARY_MODEL_PROVIDER from .env. Evals set offline explicitly,
    # so test runs never use a provider account by accident.
    llm_mode: str = field(default_factory=lambda: os.getenv("LLM_MODE", "auto").strip().lower())
    primary_model_provider: str = field(default_factory=lambda: os.getenv("PRIMARY_MODEL_PROVIDER", "gemini").strip().lower())
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    grok_api_key: str = field(default_factory=lambda: os.getenv("GROK_API_KEY", os.getenv("GROQ_API_KEY", "")))
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""))
    openrouter_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    openrouter_api_key_2: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY_2", ""))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))
    gemini_fallback_models: tuple[str, ...] = field(
        default_factory=lambda: _csv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite")
    )
    groq_models: tuple[str, ...] = field(
        default_factory=lambda: _csv(
            "GROQ_FALLBACK_MODELS",
            "groq/qwen/qwen3.8-27b,groq/openai/gpt-oss-120b,groq/openai/gpt-oss-20b",
        )
    )
    openrouter_models: tuple[str, ...] = field(
        default_factory=lambda: _csv(
            "OPENROUTER_MODELS",
            "openrouter/openrouter/free,openrouter/nvidia/nemotron-3.5-lightning:free,openrouter/liquid/lfm-2.5-2.6b:free",
        )
    )
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    anthropic_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest"))
    provider_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("LLM_PROVIDER_TIMEOUT_SECONDS", "12")))
    enable_react_loop: bool = field(
        default_factory=lambda: os.getenv("ENABLE_REACT_LOOP", "true").strip().lower() in ("true", "1", "yes")
    )
    max_model_turns_per_run: int = field(
        default_factory=lambda: int(os.getenv("MAX_MODEL_TURNS_PER_RUN", "8"))
    )
    max_tool_calls_per_run: int = 8
    max_wall_clock_seconds: float = field(
        default_factory=lambda: float(os.getenv("MAX_WALL_CLOCK_SECONDS", "35.0"))
    )
    max_context_tokens: int = field(default_factory=lambda: int(os.getenv("MAX_CONTEXT_TOKENS", "1200")))
    max_memory_tokens: int = 250
    max_user_input_tokens: int = 350
    hold_ttl_seconds: int = 600
    max_mutation_retries: int = 2

    def resolved_provider(self) -> str:
        return self.primary_model_provider if self.llm_mode == "auto" else self.llm_mode

    def is_offline(self) -> bool:
        return self.resolved_provider() == "offline"

    def get_provider_cascade_order(self) -> list[str]:
        """Return the resilient multi-tier LLM provider cascade order.
        
        If llm_mode is explicitly offline or replay, returns [self.llm_mode].
        Otherwise returns ['gemini', 'groq', 'openrouter'] (adjusting primary if specified).
        """
        if self.is_offline() or self.llm_mode in {"offline", "replay"}:
            return [self.llm_mode]
        primary = self.resolved_provider()
        default_cascade = ["gemini", "groq", "openrouter"]
        if primary in default_cascade:
            order = [primary] + [p for p in default_cascade if p != primary]
        else:
            order = [primary] + default_cascade
        return order


def get_provider_cascade_order(config: Optional[AgentConfig] = None) -> list[str]:
    cfg = config or AgentConfig()
    return cfg.get_provider_cascade_order()
