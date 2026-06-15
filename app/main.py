import logging
import threading

from app.config import Config
from app.service import TikTokToTelegram
from app.web import create_app
from waitress import serve


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_sources()
    service = TikTokToTelegram(config)
    if config.tiktok_channels:
        threading.Thread(target=service.run_forever, daemon=True, name="monitor").start()
    serve(create_app(config, service), host=config.web_host, port=config.web_port, threads=4)


if __name__ == "__main__":
    main()
