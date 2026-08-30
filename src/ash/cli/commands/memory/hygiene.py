"""Memory hygiene reporting."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ash.cli.console import console, create_table, dim, warning

if TYPE_CHECKING:
    from ash.config import AshConfig
    from ash.store.store import Store


async def memory_hygiene_report(store: Store, config: AshConfig, limit: int) -> None:
    """Show memories likely to cause unrelated context bleed."""
    entries = await store.list_memories(
        limit=max(limit * 4, limit),
        include_expired=False,
        include_superseded=False,
    )
    prefixes = tuple(
        prefix.lower() for prefix in config.memory_hygiene.sensitive_source_prefixes
    )
    suspicious = []
    for entry in entries:
        source = (entry.source or "").lower()
        content = entry.content.lower()
        if source.startswith(prefixes) or any(
            token in content
            for token in ("gmail", "calendar", "school email", "sf day", "veracross")
        ):
            suspicious.append(entry)
        if len(suspicious) >= limit:
            break

    if not suspicious:
        warning("No suspicious memory entries found")
        return

    table = create_table(
        "Memory Hygiene",
        [
            ("ID", {"style": "dim", "max_width": 8}),
            ("Source", {"style": "cyan", "max_width": 18}),
            ("Created", {"style": "dim", "max_width": 10}),
            ("Content", {"style": "white", "max_width": 80}),
        ],
    )
    for entry in suspicious:
        created = entry.created_at.strftime("%Y-%m-%d") if entry.created_at else "-"
        table.add_row(
            entry.id[:8],
            entry.source or "-",
            created,
            entry.content.replace("\n", " ")[:160],
        )
    console.print(table)
    dim(
        "\nReview these with `ash memory show <id>` and remove stale/noisy entries "
        "with `ash memory remove <id>`."
    )
