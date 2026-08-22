"""Local dev server entrypoint: `uv run python -m app.dev` (wired to `make dev`).

Use this instead of `fastapi dev` / `uvicorn app.main:app` directly on Windows
— see app/core/winloop.py for why a plain uvicorn CLI invocation there breaks
async Postgres.
"""

import sys

import uvicorn


def main() -> None:
    kwargs: dict = {}
    if sys.platform == "win32":
        from app.core.winloop import selector_loop_factory

        kwargs["loop"] = selector_loop_factory

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True, **kwargs)


if __name__ == "__main__":
    main()
