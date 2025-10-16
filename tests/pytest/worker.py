from logger import get_logger

# IMPORTANT: same logger name ("collector") → same logger instance
logger = get_logger(use_buffer=True)

def do_work():
    logger.info("Worker is running")
    logger.warning("Worker had a minor issue")
