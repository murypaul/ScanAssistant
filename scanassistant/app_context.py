"""Application composition root.

Instantiates application services and wires them together; the only
place where global mutable state is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass

from scanassistant.config import GlobalConfig, load_config


@dataclass
class AppContext:
    """Application context: global configuration, then business services."""

    config: GlobalConfig

    @classmethod
    def bootstrap(cls) -> AppContext:
        """Builds the context at startup (reads `config.json`)."""
        return cls(config=load_config())
