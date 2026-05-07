"""
Configuração centralizada de logging para o AEGIS.
Uso: from app.utils.logger import get_logger; log = get_logger(__name__)
"""
import logging
import sys


_FORMATTER = logging.Formatter(
    fmt="[%(name)s][%(levelname)s] %(message)s — %(asctime)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_FORMATTER)


def get_logger(name: str) -> logging.Logger:
    """
    Retorna um Logger configurado com o formatter padrão AEGIS.
    Idempotente: chamar múltiplas vezes com o mesmo nome retorna o mesmo logger.
    """
    logger = logging.getLogger(f"aegis.{name}")
    if not logger.handlers:
        logger.addHandler(_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
