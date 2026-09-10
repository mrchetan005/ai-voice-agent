# Prompts

`PromptTemplate` (runtime-variable text, composition, files) and `PromptConfig` (its declarative form in
[configuration](configuration.md)).

Deliberately small: stdlib `str.format` variables (`{name}`), no template engine. Missing variables raise;
extra variables are ignored — so one `prompt_vars` dict can feed a composed prompt whose parts use different
subsets.

## PromptTemplate

```python
from voiceagent import PromptTemplate

t = PromptTemplate(
    "You are a sales assistant for {company}. Speak in {language}.",
    defaults={"language": "English"},
)
t.variables            # frozenset({'company', 'language'})
t.render(company="Acme")   # -> "You are a sales assistant for Acme. Speak in English."
```

| Member | Signature | Behavior |
| --- | --- | --- |
| `__init__` | `PromptTemplate(template, defaults=None)` | parses `{variable}` placeholders into `.variables` |
| `.template` | `str` | the raw template string |
| `.defaults` | `dict[str, str]` | values used when `render` isn't passed them |
| `.variables` | `frozenset[str]` | root variable names found in the template |
| `.render(**variables)` | `-> str` | merges `defaults` then kwargs, then `str.format` |
| `.from_file(path, defaults=None)` | classmethod `-> PromptTemplate` | reads UTF-8 file as the template |
| `t1 + t2` | `-> PromptTemplate` | composition (also accepts a raw `str`) |

### Variables and rendering

Placeholders use `str.format` syntax. Attribute/index access resolves to the **root** name, so `{user.name}`
and `{items[0]}` register as variables `user` and `items`:

```python
PromptTemplate("Hi {user.name}").variables    # frozenset({'user'})
```

`render` merges `defaults` first, then the call kwargs, and formats:

- **Missing** variables (in neither defaults nor kwargs) raise `PromptError`, listing the missing names.
- **Extra** kwargs not referenced by the template are ignored.
- Literal braces are escaped as `{{` / `}}` and pass through `render` unchanged (formatting always runs, even
  with zero variables).

```python
PromptTemplate("Hello {name}").render()
# voiceagent.prompts.PromptError: missing prompt variables: name
```

### Composition

`+` joins two templates (or a template and a string) with a blank line between them; defaults merge, with the
right-hand side winning on conflicts:

```python
base = PromptTemplate("You are {agent}, a voice assistant.", {"agent": "Aria"})
rules = "Keep replies under two sentences. Never invent facts."
full = base + rules
full.variables    # frozenset({'agent'})
```

### From a file

```python
sys = PromptTemplate.from_file("prompts/sales.txt", defaults={"company": "Acme"})
```

## PromptConfig

The declarative source of a prompt inside an `AgentConfig` — inline text **or** a file, plus variables.
Exactly one of `text`/`file` must be set (validated); otherwise it raises `ValueError`.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `text` | `str` \| `None` | `None` | inline template |
| `file` | `str` \| `None` | `None` | path to a template file |
| `variables` | `dict[str, str]` | `{}` | defaults baked into the resolved template |

`.resolve()` returns a `PromptTemplate` (from `file` if set, else `text`) with `variables` as its defaults.

In YAML this is the `prompt:` block:

```yaml
prompt:
  text: |
    You are a concise, friendly sales assistant for {company}.
    Answer in at most two short sentences — this is a voice call.
  variables:
    company: Acme Corp
```

```yaml
prompt:
  file: prompts/sales.txt
  variables:
    company: Acme Corp
```

## Runtime rendering

At compile time the agent's `PromptConfig` is resolved and rendered with per-session `prompt_vars` (carried on
the dispatch [`SessionMetadata`](configuration.md)) to produce the LLM `instructions`:

```python
instructions = cfg.prompt.resolve().render(**meta.prompt_vars)
```

So YAML `variables` act as defaults and a channel's `prompt_vars` override them per call. Any variable that is
neither a default nor supplied at dispatch raises `PromptError` and fails the session — keep defaults for every
placeholder you can't guarantee at runtime.

## PromptError

`PromptError` subclasses `ValueError`. Raised for an invalid template (malformed `{...}`) at construction, and
for missing variables at `render`.
