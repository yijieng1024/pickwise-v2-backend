"""
Shared Playwright execution helpers.

Why this file exists:
Uvicorn on Windows uses SelectorEventLoop, which cannot spawn subprocesses.
Playwright needs to spawn a Chromium subprocess, so it requires
ProactorEventLoop. Instead of replacing uvicorn's main loop (which would
affect every other route/task), we offload all Playwright work to a
worker thread that owns its own fresh event loop — completely isolated
from uvicorn's loop.

Every scraper (apple_scraper, asus_scraper, future brand scrapers, ...)
should import `run_async_playwright` from here instead of re-implementing
this logic. That way the Windows-compatibility fix lives in exactly one
place.
"""

import sys
import asyncio
import concurrent.futures


def run_playwright_in_thread(coro):
    """
    Runs a single Playwright coroutine to completion inside a brand-new
    thread that owns its own event loop (ProactorEventLoop on Windows).

    This is a *blocking* call — it waits for the coroutine to finish and
    returns its result. It's meant to be called via `run_in_executor`
    (see `run_async_playwright` below), not directly from async code,
    otherwise it will block the calling event loop.
    """

    def thread_target():
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(thread_target)
        return future.result()


async def run_async_playwright(coro):
    """
    Await-friendly entry point for FastAPI route handlers / async callers.

    Usage:
        result = await run_async_playwright(_async_crawl_apple_specs_links(url))

    Internally hands the coroutine off to `run_playwright_in_thread` via
    `run_in_executor`, so it never blocks uvicorn's main event loop and
    always gets a Playwright-compatible event loop under the hood.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        run_playwright_in_thread,
        coro,
    )