"""Configuration. Everything comes from environment variables with safe defaults."""
import os
from dataclasses import dataclass


@dataclass
class Config:
    model: str = ""                     # AGENT_MODEL (required for real runs)
    max_steps: int = 25                 # AGENT_MAX_STEPS
    token_budget: int = 150_000         # AGENT_TOKEN_BUDGET (input + output, summed over all calls)
    max_output_tokens: int = 4096       # per API call
    command_timeout_default: int = 30   # seconds
    command_timeout_max: int = 60       # seconds
    max_files_changed: int = 5          # 6th distinct changed file needs confirmation

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            model=os.environ.get("AGENT_MODEL", ""),
            max_steps=int(os.environ.get("AGENT_MAX_STEPS", "25")),
            token_budget=int(os.environ.get("AGENT_TOKEN_BUDGET", "150000")),
        )
