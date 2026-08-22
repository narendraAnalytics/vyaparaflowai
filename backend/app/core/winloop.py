import asyncio


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """uvicorn>=0.36 on Windows constructs ProactorEventLoop directly as its
    loop factory (bypassing asyncio.set_event_loop_policy entirely), which
    breaks psycopg3's async mode. Pass this factory to uvicorn.run(loop=...)
    to force SelectorEventLoop instead. Only needed for local Windows dev —
    Docker/production run on Linux and are unaffected.
    """
    return asyncio.SelectorEventLoop()
