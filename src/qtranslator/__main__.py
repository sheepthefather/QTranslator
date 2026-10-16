"""允许 ``python -m qtranslator`` 启动。"""

import sys

from .app import run

if __name__ == "__main__":
    sys.exit(run())
