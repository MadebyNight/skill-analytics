"""Built-in platform adapters for Skill analytics."""

from .base import Adapter, InstalledSkill, Invocation
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .opencode import OpenCodeAdapter
from .pi import PiAdapter

__all__ = [
    "Adapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "InstalledSkill",
    "Invocation",
    "OpenCodeAdapter",
    "PiAdapter",
]
