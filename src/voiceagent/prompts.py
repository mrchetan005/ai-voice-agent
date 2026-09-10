"""Prompt management: templates with runtime variables, composition, files.

Deliberately small: stdlib ``str.format`` variables (``{name}``), no template
engine. Missing variables raise; extra variables are ignored so callers can
pass one prompt_vars dict to a composed prompt whose parts use different
subsets.
"""

from __future__ import annotations

from pathlib import Path
from string import Formatter
from typing import Any

from pydantic import BaseModel, Field, model_validator


class PromptError(ValueError):
    """Invalid template or missing variables at render time."""


def _parse_variables(template: str) -> frozenset[str]:
    names: set[str] = set()
    try:
        for _, field_name, _, _ in Formatter().parse(template):
            if field_name:
                # "{user.name}" / "{items[0]}" -> root variable "user" / "items"
                names.add(field_name.split(".")[0].split("[")[0])
    except ValueError as exc:
        raise PromptError(f"invalid prompt template: {exc}") from exc
    return frozenset(names)


class PromptTemplate:
    """A prompt with ``{variable}`` placeholders and optional defaults."""

    def __init__(self, template: str, defaults: dict[str, str] | None = None) -> None:
        self.template = template
        self.defaults = dict(defaults or {})
        self.variables = _parse_variables(template)

    @classmethod
    def from_file(cls, path: str | Path, defaults: dict[str, str] | None = None) -> PromptTemplate:
        return cls(Path(path).read_text(encoding="utf-8"), defaults)

    def render(self, **variables: Any) -> str:
        merged = {**self.defaults, **variables}
        missing = sorted(self.variables - merged.keys())
        if missing:
            raise PromptError(f"missing prompt variables: {', '.join(missing)}")
        # Always format (even with zero variables) so "{{" escaping behaves
        # identically whether or not the template also has variables.
        return self.template.format(**merged)

    def __add__(self, other: PromptTemplate | str) -> PromptTemplate:
        if isinstance(other, str):
            other = PromptTemplate(other)
        return PromptTemplate(
            f"{self.template}\n\n{other.template}",
            {**self.defaults, **other.defaults},
        )

    def __repr__(self) -> str:
        return f"PromptTemplate(variables={sorted(self.variables)!r})"


class PromptConfig(BaseModel):
    """Declarative prompt source: inline text or a template file."""

    text: str | None = None
    file: str | None = None
    variables: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> PromptConfig:
        if bool(self.text) == bool(self.file):
            raise ValueError("prompt config needs exactly one of 'text' or 'file'")
        return self

    def resolve(self) -> PromptTemplate:
        if self.file:
            return PromptTemplate.from_file(self.file, self.variables)
        assert self.text is not None
        return PromptTemplate(self.text, self.variables)
