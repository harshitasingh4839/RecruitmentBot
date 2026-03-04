from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from dateutil import parser as dtparser
import logging
logger = logging.getLogger(__name__)

def _parse_local(dt_str: str) -> datetime:
    # Expected format: YYYY-MM-DDTHH:MM (no timezone). Used only for display grouping.
    return dtparser.isoparse(dt_str)

def format_slots_preview(slots: list[dict], max_per_day: int = 12) -> str:
    """Return a compact, chat-friendly preview grouped by date.

    Example:
    2026-03-02:
      10:00-10:30
      10:40-11:10
      +3 more
    """
    logger.info(f"Formatting preview for {len(slots)} slots")
    by_date: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)

    for s in slots:
        st = _parse_local(s["startAtLocal"])
        en = _parse_local(s["endAtLocal"])
        by_date[st.date().isoformat()].append((st, en))

    if not by_date:
        return "No slots to preview."

    lines: list[str] = []
    for d in sorted(by_date.keys()):
        items = sorted(by_date[d], key=lambda x: x[0])
        lines.append(f"{d}:")
        for idx, (st, en) in enumerate(items):
            if idx >= max_per_day:
                remaining = len(items) - max_per_day
                if remaining > 0:
                    lines.append(f"  +{remaining} more")
                break
            lines.append(f"  {st.strftime('%H:%M')}-{en.strftime('%H:%M')}")
    return "\n".join(lines)

