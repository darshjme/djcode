"""Usage stats with GitHub-style activity heatmap for DJcode.

Tracks sessions, tokens, models, and renders a rich TUI dashboard
with /stats command. All data stored locally in ~/.djcode/stats.json.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from djcode.config import CONFIG_DIR

GOLD = "#FFD700"
STATS_FILE = CONFIG_DIR / "stats.json"

# Heatmap intensity blocks (light to dark gold)
HEAT_BLOCKS = [" ", "\u2591", "\u2592", "\u2593", "\u2588"]  # ░▒▓█
HEAT_COLORS = ["dim", "#4a3800", "#7a5f00", "#b88f00", "#FFD700"]


def _load_stats() -> dict[str, Any]:
    """Load stats from disk."""
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"sessions": [], "version": 1}


def _save_stats(data: dict[str, Any]) -> None:
    """Save stats to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATS_FILE.write_text(json.dumps(data, indent=2))


def record_session_start(model: str, provider: str) -> str:
    """Record the start of a new session. Returns session ID."""
    data = _load_stats()
    session_id = f"s_{int(time.time())}"
    session = {
        "id": session_id,
        "start": datetime.now().isoformat(),
        "end": None,
        "model": model,
        "provider": provider,
        "tokens": 0,
        "messages": 0,
        "tools_used": 0,
    }
    data["sessions"].append(session)
    _save_stats(data)
    return session_id


def record_session_update(
    session_id: str,
    *,
    tokens: int = 0,
    messages: int = 0,
    tools_used: int = 0,
) -> None:
    """Update a running session's counters."""
    data = _load_stats()
    for s in reversed(data["sessions"]):
        if s["id"] == session_id:
            s["tokens"] = s.get("tokens", 0) + tokens
            s["messages"] = s.get("messages", 0) + messages
            s["tools_used"] = s.get("tools_used", 0) + tools_used
            break
    _save_stats(data)


def record_session_end(session_id: str) -> None:
    """Mark a session as ended."""
    data = _load_stats()
    for s in reversed(data["sessions"]):
        if s["id"] == session_id:
            s["end"] = datetime.now().isoformat()
            break
    _save_stats(data)


# ── Heatmap rendering ──────────────────────────────────────────────────────

def _build_heatmap(sessions: list[dict], days: int = 365) -> list[str]:
    """Build a GitHub-style contribution heatmap.

    Returns lines of text for the heatmap grid.
    """
    today = datetime.now().date()
    start_date = today - timedelta(days=days - 1)

    # Count tokens per day
    day_tokens: Counter[str] = Counter()
    for s in sessions:
        try:
            d = datetime.fromisoformat(s["start"]).date()
            day_tokens[d.isoformat()] += s.get("tokens", 0)
        except (ValueError, KeyError):
            continue

    # Determine intensity thresholds
    values = [v for v in day_tokens.values() if v > 0]
    if values:
        max_val = max(values)
        thresholds = [0, max_val * 0.25, max_val * 0.5, max_val * 0.75, max_val]
    else:
        thresholds = [0, 1, 2, 3, 4]

    def intensity(count: int) -> int:
        if count == 0:
            return 0
        for i, t in enumerate(thresholds[1:], 1):
            if count <= t:
                return i
        return 4

    # Build grid: 7 rows (Mon-Sun) x N weeks
    # Start from the Monday of start_date's week
    days_since_monday = start_date.weekday()  # 0=Mon
    grid_start = start_date - timedelta(days=days_since_monday)

    weeks: list[list[int]] = []
    current = grid_start
    while current <= today:
        week: list[int] = []
        for _ in range(7):
            if current < start_date or current > today:
                week.append(-1)  # out of range
            else:
                count = day_tokens.get(current.isoformat(), 0)
                week.append(intensity(count))
            current += timedelta(days=1)
        weeks.append(week)

    # Render month labels
    month_labels = []
    prev_month = -1
    for i, w_start in enumerate(weeks):
        d = grid_start + timedelta(weeks=i)
        if d.month != prev_month and d >= start_date:
            month_labels.append((i, d.strftime("%b")))
            prev_month = d.month

    # Build output lines
    lines: list[str] = []

    # Month header
    header = "     "
    label_positions = {pos: label for pos, label in month_labels}
    for i in range(len(weeks)):
        if i in label_positions:
            lbl = label_positions[i]
            header += lbl
            header += " " * max(0, 2 - len(lbl))
        else:
            header += "  "
    lines.append(header.rstrip())

    # Day rows
    day_names = ["Mon", "   ", "Wed", "   ", "Fri", "   ", "Sun"]
    for row in range(7):
        line = f"{day_names[row]} "
        for week in weeks:
            val = week[row] if row < len(week) else -1
            if val < 0:
                line += "  "
            else:
                line += HEAT_BLOCKS[val] + " "
        lines.append(line.rstrip())

    # Legend
    legend = "     Less "
    for i in range(5):
        legend += HEAT_BLOCKS[i]
    legend += " More"
    lines.append("")
    lines.append(legend)

    return lines


def _compute_streaks(sessions: list[dict]) -> tuple[int, int]:
    """Compute longest streak and current streak of active days."""
    if not sessions:
        return 0, 0

    active_days: set[str] = set()
    for s in sessions:
        try:
            d = datetime.fromisoformat(s["start"]).date()
            active_days.add(d.isoformat())
        except (ValueError, KeyError):
            continue

    if not active_days:
        return 0, 0

    sorted_days = sorted(active_days)
    dates = [datetime.fromisoformat(d).date() for d in sorted_days]

    # Longest streak
    longest = 1
    current = 1
    for i in range(1, len(dates)):
        if (dates[i] - dates[i - 1]).days == 1:
            current += 1
            longest = max(longest, current)
        elif (dates[i] - dates[i - 1]).days > 1:
            current = 1

    # Current streak (counting back from today)
    today = datetime.now().date()
    current_streak = 0
    check = today
    while check.isoformat() in active_days:
        current_streak += 1
        check -= timedelta(days=1)

    return longest, current_streak


def _format_duration(seconds: float) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    hours = seconds / 3600
    if hours < 24:
        return f"{int(hours)}h {int((seconds % 3600) // 60)}m"
    days = hours / 24
    remaining_hours = hours % 24
    return f"{int(days)}d {int(remaining_hours)}h"


def _format_tokens(count: int) -> str:
    """Format token count with suffix."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}m"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


# ── Main render ────────────────────────────────────────────────────────────

def render_stats(console: Console, period: str = "all") -> None:
    """Render the full stats dashboard."""
    data = _load_stats()
    sessions = data.get("sessions", [])

    if not sessions:
        console.print(f"[{GOLD}]No usage data yet.[/] Start using DJcode to see stats here.")
        return

    # Filter by period
    now = datetime.now()
    if period == "7d":
        cutoff = now - timedelta(days=7)
        sessions = [s for s in sessions if datetime.fromisoformat(s["start"]) >= cutoff]
        period_label = "Last 7 days"
    elif period == "30d":
        cutoff = now - timedelta(days=30)
        sessions = [s for s in sessions if datetime.fromisoformat(s["start"]) >= cutoff]
        period_label = "Last 30 days"
    else:
        period_label = "All time"

    # Compute stats
    total_tokens = sum(s.get("tokens", 0) for s in sessions)
    total_messages = sum(s.get("messages", 0) for s in sessions)
    total_tools = sum(s.get("tools_used", 0) for s in sessions)
    num_sessions = len(sessions)

    # Session durations
    durations: list[float] = []
    for s in sessions:
        if s.get("end"):
            try:
                start = datetime.fromisoformat(s["start"])
                end = datetime.fromisoformat(s["end"])
                durations.append((end - start).total_seconds())
            except ValueError:
                pass

    longest_session = max(durations) if durations else 0

    # Active days
    active_days: set[str] = set()
    for s in sessions:
        try:
            d = datetime.fromisoformat(s["start"]).date()
            active_days.add(d.isoformat())
        except (ValueError, KeyError):
            continue

    # Most active day
    day_counts: Counter[str] = Counter()
    for s in sessions:
        try:
            d = datetime.fromisoformat(s["start"]).date()
            day_counts[d.isoformat()] += s.get("tokens", 0)
        except (ValueError, KeyError):
            continue

    most_active = max(day_counts, key=day_counts.get) if day_counts else "N/A"
    if most_active != "N/A":
        try:
            most_active = datetime.fromisoformat(most_active).strftime("%b %d")
        except ValueError:
            pass

    # Favorite model
    model_counts: Counter[str] = Counter()
    for s in sessions:
        model_counts[s.get("model", "unknown")] += s.get("tokens", 0)
    fav_model = model_counts.most_common(1)[0][0] if model_counts else "unknown"

    # Streaks
    all_sessions = data.get("sessions", [])  # use full data for streaks
    longest_streak, current_streak = _compute_streaks(all_sessions)

    # Days in range
    if period == "7d":
        total_days = 7
    elif period == "30d":
        total_days = 30
    else:
        if sessions:
            first = datetime.fromisoformat(sessions[0]["start"]).date()
            total_days = (datetime.now().date() - first).days + 1
        else:
            total_days = 1

    # ── Render ─────────────────────────────────────────────────────────

    output = Text()

    # Period selector
    periods = [("All time", "all"), ("Last 7 days", "7d"), ("Last 30 days", "30d")]
    period_line = Text()
    for label, key in periods:
        if key == period:
            period_line.append(f" {label} ", style=f"bold {GOLD} on #333333")
        else:
            period_line.append(f" {label} ", style="dim")
        period_line.append(" \u00b7 ", style="dim")

    console.print()
    console.print(period_line)
    console.print()

    # Heatmap (only for all-time view)
    if period == "all":
        heatmap_lines = _build_heatmap(all_sessions, days=365)
        for line in heatmap_lines:
            heatmap_text = Text(line)
            # Color the block characters
            for i, ch in enumerate(line):
                if ch in HEAT_BLOCKS[1:]:
                    idx = HEAT_BLOCKS.index(ch)
                    heatmap_text.stylize(HEAT_COLORS[idx], i, i + 1)
            console.print(heatmap_text)
        console.print()

    # Stats grid (2-column layout)
    stats_table = Table(show_header=False, box=None, padding=(0, 3))
    stats_table.add_column(style="white", min_width=30)
    stats_table.add_column(style="white", min_width=30)

    stats_table.add_row(
        Text.assemble(("Favorite model: ", "bold"), (fav_model, f"bold {GOLD}")),
        Text.assemble(("Total tokens: ", "bold"), (_format_tokens(total_tokens), f"bold {GOLD}")),
    )
    stats_table.add_row(Text(), Text())  # spacer
    stats_table.add_row(
        Text.assemble(("Sessions: ", "bold"), (str(num_sessions), f"bold {GOLD}")),
        Text.assemble(("Longest session: ", "bold"), (_format_duration(longest_session), f"bold {GOLD}")),
    )
    stats_table.add_row(
        Text.assemble(("Active days: ", "bold"), (f"{len(active_days)}/{total_days}", f"bold {GOLD}")),
        Text.assemble(("Longest streak: ", "bold"), (f"{longest_streak} days", f"bold {GOLD}")),
    )
    stats_table.add_row(
        Text.assemble(("Most active day: ", "bold"), (most_active, f"bold {GOLD}")),
        Text.assemble(("Current streak: ", "bold"), (f"{current_streak} days", f"bold {GOLD}")),
    )
    stats_table.add_row(Text(), Text())  # spacer
    stats_table.add_row(
        Text.assemble(("Messages: ", "bold"), (str(total_messages), f"bold {GOLD}")),
        Text.assemble(("Tool calls: ", "bold"), (str(total_tools), f"bold {GOLD}")),
    )

    console.print(stats_table)
    console.print()

    # Fun comparison
    if total_tokens > 0:
        # The Little Prince ≈ 22,000 tokens
        book_multiple = total_tokens / 22000
        if book_multiple >= 1:
            console.print(
                f"  [{GOLD}]You've used ~{int(book_multiple)}x more tokens than The Little Prince[/]"
            )
        console.print()

    # Keyboard hints
    console.print("  [dim]/stats \u00b7 /stats 7d \u00b7 /stats 30d[/]")
    console.print()
