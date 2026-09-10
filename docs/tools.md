# Tools

Plain Python functions the agent can call: the `@tool` decorator, `Tool`, `ToolContext`, and `ToolFailure`.

Tools are engine-neutral — they receive a `ToolContext` (never a LiveKit type) and raise `ToolFailure` for
errors that should be spoken back to the caller. The livekit compile seam wraps a `Tool` into the engine's
native function-tool format (see [concepts](concepts.md)).

## @tool

```python
from voiceagent import tool, ToolContext, ToolFailure

@tool(description="List available appointment slots for a weekday")
async def get_available_slots(ctx: ToolContext, weekday: str) -> str:
    slots = CALENDAR.get(weekday.lower())
    if slots is None:
        raise ToolFailure(f"I don't have a calendar for {weekday}.")
    return f"Available on {weekday}: {', '.join(slots)}"
```

Two forms — bare or parametrized:

```python
@tool                                    # description taken from the docstring's first line
def current_datetime() -> str:           # sync tools work too
    "Get today's date and the current time"
    return dt.datetime.now(dt.UTC).strftime("%A %Y-%m-%d %H:%M UTC")

@tool(name="lookup", description="Look up a SKU price", timeout_s=5.0)
async def check_price(ctx: ToolContext, sku: str) -> str:
    ...
```

| Argument | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` \| `None` | function `__name__` | tool name the LLM sees |
| `description` | `str` \| `None` | first line of the docstring | **required** — raises if neither is present |
| `timeout_s` | `float` | `15.0` | per-invocation timeout |

### Rules the decorator enforces

- **Description required.** Pass `description=` or give the function a docstring; a tool with neither raises
  `ValueError`.
- **Type annotations required.** Every parameter (except the context) must be annotated so its JSON schema can
  be derived; an un-annotated parameter raises `ValueError`.
- **Context is optional and detected by the first parameter.** If the first parameter is annotated `ToolContext`
  **or** named `ctx`, it's treated as the injected context and stripped from the LLM-visible signature. Tools
  that don't need it just omit that parameter (like `current_datetime` above).

## ToolContext

What a tool can see and do, independent of the engine. Injected per call — not part of the LLM schema.

| Field | Type | Notes |
| --- | --- | --- |
| `ids` | `SessionIDs` | stable identifiers for this session |
| `memory` | `Memory` \| `None` | conversation store, or `None` if the agent has no memory ([memory](memory.md)) |
| `userdata` | `dict[str, Any]` | per-session scratch space shared across tool calls |
| `emit` | `Callable[[BaseEvent], None]` | emit a canonical event onto the session's event stream |

`SessionIDs` carries: `tenant_id`, `agent_id`, `session_id`, `room_id`, `call_id`, `user_id`, `channel`.

```python
@tool(description="Book an appointment slot for the caller")
async def book_appointment(ctx: ToolContext, weekday: str, time: str, name: str) -> str:
    slots = CALENDAR.get(weekday.lower(), [])
    if time not in slots:
        raise ToolFailure(f"{time} on {weekday} is not available.")
    slots.remove(time)
    booked_for = ctx.ids.user_id or name
    return f"Booked {weekday} {time} for {booked_for}."
```

## ToolFailure

Raise `ToolFailure(message, detail=None)` for an error the model should hear and relay. `message` is
spoken-safe; `detail` is for logs only and never reaches the model or the user.

```python
raise ToolFailure("That slot is taken.", detail=f"conflict on {row_id}")
```

At runtime the wrapper maps outcomes onto the engine's `ToolError`:

| Outcome | What the caller hears |
| --- | --- |
| `ToolFailure` raised | your `message` |
| timeout (`> timeout_s`) | `"That took too long — please try again."` |
| any other exception | `"Something went wrong with that request."` |

Every call emits `ToolCallStarted` before and `ToolCallFinished` after (with `result`, `error`, and
`duration_ms`) onto the event stream — see [observability](observability.md).

## Execution model

- **Sync or async.** Coroutine tools are awaited; plain functions run in a worker thread
  (`asyncio.to_thread`), so a blocking tool won't stall the event loop.
- **Timeout.** Each invocation runs under `asyncio.timeout(timeout_s)`; on expiry the caller hears the timeout
  message above.
- **Schema.** The LLM sees the tool's `name`, `description`, and its parameters minus the context, with types
  from your annotations. Return a `str` (or any value; it's stringified into the event).

## Wiring tools into an agent

In Python, pass `Tool` objects (or bare callables, auto-wrapped) to `VoiceAgent(tools=[...])` or `add_tool()`.
In [YAML / config](configuration.md), list dotted paths `"pkg.module:attr"` under `tools:`; each must resolve
to a `Tool` or a callable:

```yaml
tools:
  - examples.tools_demo:get_available_slots
  - examples.tools_demo:book_appointment
  - examples.tools_demo:current_datetime
```

See `examples/tools_demo.py` for the full, runnable set.
