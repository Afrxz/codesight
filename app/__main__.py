"""Start the app:  python -m app  [--port 8000] [--no-browser]"""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

from . import config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="RAGG: ask questions about a codebase.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser")
    args = parser.parse_args(argv)

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(config.DATA_DIR / "ragg.log", encoding="utf-8")])
    for noisy in ("httpx", "LiteLLM", "litellm", "huggingface_hub", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    import uvicorn
    from .api import app, warm_up

    url = f"http://127.0.0.1:{args.port}"
    logging.getLogger("ragg").info("RAGG running at %s (Ctrl+C to stop)", url)
    warm_up()
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
