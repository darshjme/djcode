"""Session-owned browser and desktop control; every call uses normal tool approval."""

from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import urlparse

from djcode.config import CONFIG_DIR


class Computer:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.pages = {}
        self.images = []

    async def browser_action(
        self, action, tab="", url="", selector="", text="", key="", headless=False
    ):
        if action == "close":
            if tab:
                await self.pages.pop(tab).close()
            else:
                await self.close()
            return "Browser closed"
        if action == "tabs":
            return json.dumps(
                [{"id": ident, "url": page.url} for ident, page in self.pages.items()]
            )
        if action == "open":
            if urlparse(url).scheme not in {"http", "https"}:
                raise ValueError("Browser navigation requires an http(s) URL")
            if not self.browser:
                try:
                    from playwright.async_api import async_playwright
                except ImportError:
                    raise RuntimeError(
                        "Install DJcode's computer extra and run: python -m playwright "
                        "install chromium"
                    ) from None
                self.playwright = await async_playwright().start()
                try:
                    self.browser = await self.playwright.chromium.launch(headless=headless)
                except BaseException:
                    await self.playwright.stop()
                    self.playwright = None
                    raise
            page = await self.browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_default_timeout(10000)
            tab = uuid.uuid4().hex[:8]
            self.pages[tab] = page
            await page.goto(url, wait_until="domcontentloaded")
        else:
            if tab not in self.pages:
                raise ValueError("Unknown tab; use browser tabs/open first")
            page = self.pages[tab]
            if action == "click":
                await page.locator(selector).click()
            elif action == "fill":
                await page.locator(selector).fill(text)
            elif action == "press":
                await page.keyboard.press(key)
            elif action == "navigate":
                if urlparse(url).scheme not in {"http", "https"}:
                    raise ValueError("Browser navigation requires an http(s) URL")
                await page.goto(url, wait_until="domcontentloaded")
            elif action == "back":
                await page.go_back(wait_until="domcontentloaded")
            elif action == "screenshot":
                path = self._image_path()
                await page.screenshot(path=str(path))
                self.images.append(str(path))
                return json.dumps({"tab": tab, "screenshot": str(path), "url": page.url})
            elif action != "snapshot":
                raise ValueError("Unknown browser action")
        return json.dumps(
            {
                "tab": tab,
                "url": page.url,
                "snapshot": (await page.locator("body").aria_snapshot())[:30000],
            }
        )

    def _image_path(self):
        root = CONFIG_DIR / "screenshots"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root / f"{uuid.uuid4().hex}.png"

    async def desktop_action(self, action, x=0, y=0, text="", keys=None, amount=0):
        try:
            import pyautogui as gui
        except ImportError:
            raise RuntimeError(
                "Desktop control requires DJcode's computer extra and OS "
                "accessibility/screen-recording permission"
            ) from None
        # Never disable PyAutoGUI's corner failsafe.
        gui.FAILSAFE = True
        if action == "screenshot":
            path = self._image_path()
            await asyncio.to_thread(lambda: gui.screenshot().save(path))
            self.images.append(str(path))
            return json.dumps({"screenshot": str(path), "size": list(gui.size())})
        if action == "size":
            return json.dumps({"size": list(gui.size()), "position": list(gui.position())})
        if action in {"click", "move"}:
            width, height = gui.size()
            if not 0 <= x < width or not 0 <= y < height:
                raise ValueError("Coordinates outside screen")
            await asyncio.to_thread(gui.click if action == "click" else gui.moveTo, x, y)
        elif action == "type":
            if not text.isascii():
                raise ValueError(
                    "Desktop type currently supports ASCII; use browser fill or a "
                    "native MCP tool for Unicode"
                )
            await asyncio.to_thread(gui.write, text, 0.01)
        elif action == "key":
            if not keys or len(keys) > 5 or any(k not in gui.KEYBOARD_KEYS for k in keys):
                raise ValueError("Provide 1..5 valid keyboard keys")
            await asyncio.to_thread(gui.hotkey, *keys)
        elif action == "scroll":
            if not isinstance(amount, int) or abs(amount) > 100:
                raise ValueError("Scroll amount must be -100..100")
            await asyncio.to_thread(gui.scroll, amount)
        else:
            raise ValueError("Unknown desktop action")
        return "Desktop action completed; take a screenshot to verify"

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            self.browser = None
            self.pages.clear()
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
