"""Rich terminal conversation output that reflows when its pane changes width."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import RichLog


class ConversationLog(RichLog):
    """Retain renderables so opening a sidebar does not crop existing replies."""

    def __init__(self, **kwargs):
        super().__init__(min_width=1, **kwargs)
        self._entries: list[object] = []
        self._last_width: int | None = None

    def write(self, content, width=None, expand=False, shrink=True, scroll_end=None, animate=False):
        # RichLog replays deferred writes after its initial size becomes known.
        # Record them then, rather than recording both the request and its replay.
        if self._size_known:
            self._entries.append(content.copy() if isinstance(content, Text) else content)
        return super().write(content, width, expand, shrink, scroll_end, animate)

    def clear(self):
        self._entries.clear()
        return super().clear()

    def on_resize(self, event):
        previous = self._last_width
        self._last_width = event.size.width
        super().on_resize(event)
        if previous is not None and previous != event.size.width:
            self.call_after_refresh(self._reflow)

    def _reflow(self) -> None:
        at_end = self.is_vertical_scroll_end
        position = self.scroll_y
        super().clear()
        for content in self._entries:
            super().write(content, shrink=True, scroll_end=False)
        if at_end:
            self.scroll_end(animate=False)
        else:
            self.scroll_to(y=position, animate=False)
