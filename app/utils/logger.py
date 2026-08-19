"""
Configuração centralizada de logging para o AEGIS.
Uso: from app.utils.logger import get_logger; log = get_logger(__name__)
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_DEFAULT_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "aegis.log")
_LOG_PATH = os.getenv("LOG_PATH", _DEFAULT_LOG_PATH)

_FORMATTER = logging.Formatter(
    fmt="[%(name)s][%(levelname)s] %(message)s — %(asctime)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_FORMATTER)

_file_handler = RotatingFileHandler(_LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
_file_handler.setFormatter(_FORMATTER)


def get_logger(name: str) -> logging.Logger:
    """
    Retorna um Logger configurado com o formatter padrão AEGIS.
    Grava em stdout e em aegis.log (persistente, sobrevive ao fim do processo).
    Idempotente: chamar múltiplas vezes com o mesmo nome retorna o mesmo logger.
    """
    logger = logging.getLogger(f"aegis.{name}")
    if not logger.handlers:
        logger.addHandler(_stream_handler)
        logger.addHandler(_file_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
