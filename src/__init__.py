from __future__ import annotations

import logging

logger = logging.getLogger()
handler = logging.StreamHandler()
formatter = logging.Formatter("Variants-Index: %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)
