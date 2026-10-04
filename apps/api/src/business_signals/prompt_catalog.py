"""Versioned, package-local instructions for structured LLM calls."""

from __future__ import annotations

import re
from functools import lru_cache
from importlib.resources import files


class PromptCatalog:
    @staticmethod
    @lru_cache(maxsize=64)
    def load(name: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", name):
            raise ValueError("Invalid prompt name")
        resource = files("business_signals.prompts").joinpath(f"{name}.txt")
        try:
            value = resource.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise RuntimeError(f"Prompt file is missing: {name}.txt") from exc
        if not value:
            raise RuntimeError(f"Prompt file is empty: {name}.txt")
        return value


prompts = PromptCatalog()
