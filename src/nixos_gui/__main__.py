# entry point - load env, start hyperdiv server
import logging
import os
import sys
from pathlib import Path

import hyperdiv as hd

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _load_env():
    # loads .env from project root if present. python-dotenv handles quoting, escapes, comments
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    load_dotenv(env_path)


def main():
    # called by hd.run()
    from nixos_gui.app import main as app_main
    return app_main()


if __name__ == "__main__":
    _load_env()
    port = int(os.environ.get("HD_PORT", "8888"))
    sys.stderr.write(f"nixos-gui: http://localhost:{port}\n")
    hd.run(main)
