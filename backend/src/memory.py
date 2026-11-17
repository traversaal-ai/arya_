"""Long-term memory via the hosted Mem0 platform (https://app.mem0.ai).

Design rules, mirrored from the rest of the pipeline:
- recall runs once at session start behind a hard timeout — a slow or down
  Mem0 must never delay the greeting;
- writes are fire-and-forget — a failed add must never break a live call.
"""

import asyncio
import logging

import catalog

logger = logging.getLogger("memory")


class Memory:
    def __init__(
        self,
        api_key: str,
        user_id: str,
        agent_id: str | None = None,
        scope: str = "agent",
    ):
        # Imported lazily so the worker starts fine without mem0 installed
        # or configured.
        from mem0 import AsyncMemoryClient

        self._client = AsyncMemoryClient(api_key=api_key)
        self.user_id = user_id
        # scope "agent": memories are private to this agent (user_id +
        # agent_id). scope "user": shared across all agents for this user.
        self.agent_id = agent_id if scope == "agent" else None

    def _filters(self) -> dict:
        if self.agent_id:
            return {"AND": [{"user_id": self.user_id}, {"agent_id": self.agent_id}]}
        return {"user_id": self.user_id}

    async def recall(self, query: str, limit: int = 5, timeout: float = 2.0) -> list[str]:
        """Top memories for this user (and agent, when scoped), or [] on
        any failure."""
        try:
            # Current mem0 clients reject top-level entity kwargs in
            # search(); the scope must ride in `filters`.
            result = await asyncio.wait_for(
                self._client.search(query, filters=self._filters()),
                timeout=timeout,
            )
        except Exception as e:
            logger.warning(f"memory recall skipped: {type(e).__name__}: {e}")
            return []
        # The client has returned both a bare list and {"results": [...]}
        # across versions — accept either.
        items = result.get("results") if isinstance(result, dict) else result
        out = []
        for item in items or []:
            text = item.get("memory") if isinstance(item, dict) else None
            if text:
                out.append(text)
        return out[:limit]

    def remember(self, role: str, content: str) -> None:
        """Queue a turn for fact extraction; never awaited on the hot path."""
        if not content:
            return

        async def _add():
            try:
                kwargs = {"user_id": self.user_id}
                if self.agent_id:
                    kwargs["agent_id"] = self.agent_id
                await self._client.add(
                    [{"role": role, "content": content}], **kwargs
                )
            except Exception as e:
                logger.warning(f"memory add skipped: {type(e).__name__}: {e}")

        asyncio.create_task(_add())


async def purge_agent(api_key: str, agent_id: str, timeout: float = 10.0) -> None:
    """Delete every memory scoped to this agent (all users) — the cascade
    behind deleting an agent. Raises on failure; the caller decides whether
    that's fatal."""
    from mem0 import AsyncMemoryClient

    client = AsyncMemoryClient(api_key=api_key)
    await asyncio.wait_for(client.delete_all(agent_id=agent_id), timeout=timeout)


async def list_for_user(api_key: str, user_id: str, timeout: float = 10.0) -> list[dict]:
    """Everything Mem0 holds about one visitor, across every agent (the
    Visitors screen shows a person, not a bucket). Entity scoping rides in
    `filters`, same rule as recall(). Raises on failure."""
    from mem0 import AsyncMemoryClient

    client = AsyncMemoryClient(api_key=api_key)
    result = await asyncio.wait_for(
        client.get_all(filters={"user_id": user_id}), timeout=timeout
    )
    # Bare list or {"results": [...]}, as recall() tolerates.
    items = result.get("results") if isinstance(result, dict) else result
    out = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("memory"):
            continue
        out.append({
            "id": item.get("id"),
            "memory": item["memory"],
            "agent_id": item.get("agent_id"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        })
    return out


async def delete_memory(api_key: str, memory_id: str, timeout: float = 10.0) -> None:
    """Delete one memory by id. Callers must first confirm the memory
    belongs to the visitor they are acting on (see api.py): a bare id is
    not scoped to anyone. Raises on failure."""
    from mem0 import AsyncMemoryClient

    client = AsyncMemoryClient(api_key=api_key)
    await asyncio.wait_for(client.delete(memory_id), timeout=timeout)


async def update_memory(
    api_key: str, memory_id: str, text: str, timeout: float = 10.0
) -> None:
    """Rewrite one memory's text. Same ownership rule as delete_memory.
    Raises on failure."""
    from mem0 import AsyncMemoryClient

    client = AsyncMemoryClient(api_key=api_key)
    await asyncio.wait_for(client.update(memory_id, text=text), timeout=timeout)


async def purge_user(api_key: str, user_id: str, timeout: float = 10.0) -> None:
    """Delete every memory about one visitor, across every agent: the
    point of forgetting a person. Raises on failure."""
    from mem0 import AsyncMemoryClient

    client = AsyncMemoryClient(api_key=api_key)
    await asyncio.wait_for(client.delete_all(user_id=user_id), timeout=timeout)


def memory_block(memories: list[str]) -> str:
    """Instructions block appended to the system prompt at session start.
    Header and usage note come from the prompt_blocks store."""
    if not memories:
        return ""
    blocks = catalog.get("prompt_blocks")
    lines = "\n".join(f"- {m}" for m in memories)
    return (
        "\n\n" + blocks["memory_header"] + "\n\n"
        + lines
        + "\n\n" + blocks["memory_usage_note"]
    )
