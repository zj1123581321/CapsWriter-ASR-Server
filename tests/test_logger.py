# coding: utf-8

import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.logger import Logger


def test_file_logger_uses_dated_rotating_handler(tmp_path):
    logger_name = "test-dated-rotating"
    logger = Logger.setup(
        logger_name,
        log_dir=str(tmp_path),
        level="INFO",
        max_bytes=1024,
        log_filename="dated",
    )

    try:
        file_handlers = [handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)]

        assert len(file_handlers) == 1
        assert type(file_handlers[0]) is RotatingFileHandler
        assert file_handlers[0].backupCount == 5

        logger.info("date format smoke")
        for handler in logger.handlers:
            handler.flush()

        content = (tmp_path / "dated_latest.log").read_text(encoding="utf-8")
        assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} INFO", content)
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        Logger._loggers.pop(logger_name, None)


def test_default_log_dir_is_repo_root(tmp_path):
    """log_dir=None 时默认目录为仓库根 logs/。"""
    logger_name = "test-default-dir"
    logger = Logger.setup(logger_name, log_dir=None, level="INFO")
    log_file = Path(__file__).resolve().parents[1] / "logs" / "test-default-dir_latest.log"
    try:
        handler = next(h for h in logger.handlers if isinstance(h, RotatingFileHandler))
        assert Path(handler.baseFilename) == log_file
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        Logger._loggers.pop(logger_name, None)
        log_file.unlink(missing_ok=True)
