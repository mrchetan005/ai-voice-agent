"""Provider registry: string names -> provider factories.

Adding a provider is: write a factory taking a :class:`ProviderSpec`, register
it under a name, done — no core changes. Built-in factories live in
``voiceagent.livekit.providers`` and are registered lazily so the core never
imports provider SDKs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["llm", "stt", "tts", "realtime", "vad"]
KINDS: tuple[Kind, ...] = ("llm", "stt", "tts", "realtime", "vad")


@dataclass(slots=True)
class ProviderSpec:
    """Everything a factory may need to build a provider instance."""

    model: str | None = None
    language: str = "en"
    voice: str | None = None
    temperature: float | None = None
    options: dict[str, Any] = field(default_factory=dict)


Factory = Callable[[ProviderSpec], Any]


class UnknownProviderError(KeyError):
    def __init__(self, kind: Kind, name: str, known: list[str]) -> None:
        super().__init__(
            f"unknown {kind} provider '{name}'; registered: {', '.join(known) or '(none)'}"
        )
        self.kind = kind
        self.name = name


class ProviderRegistry:
    def __init__(self) -> None:
        self._factories: dict[tuple[Kind, str], Factory] = {}

    def register(self, kind: Kind, name: str, factory: Factory, *, overwrite: bool = False) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown provider kind '{kind}'; expected one of {KINDS}")
        key = (kind, name)
        if key in self._factories and not overwrite:
            raise ValueError(f"{kind} provider '{name}' is already registered")
        self._factories[key] = factory

    def create(self, kind: Kind, name: str, spec: ProviderSpec | None = None) -> Any:
        factory = self._factories.get((kind, name))
        if factory is None:
            raise UnknownProviderError(kind, name, self.names(kind))
        return factory(spec or ProviderSpec())

    def names(self, kind: Kind) -> list[str]:
        return sorted(n for k, n in self._factories if k == kind)

    def __contains__(self, key: tuple[Kind, str]) -> bool:
        return key in self._factories


registry = ProviderRegistry()
"""Module-level default registry; most applications use only this one."""


def _register_decorator(kind: Kind) -> Callable[[str], Callable[[Factory], Factory]]:
    def outer(name: str, *, overwrite: bool = False) -> Callable[[Factory], Factory]:
        def inner(factory: Factory) -> Factory:
            registry.register(kind, name, factory, overwrite=overwrite)
            return factory

        return inner

    return outer


register_llm = _register_decorator("llm")
register_stt = _register_decorator("stt")
register_tts = _register_decorator("tts")
register_realtime = _register_decorator("realtime")
register_vad = _register_decorator("vad")
