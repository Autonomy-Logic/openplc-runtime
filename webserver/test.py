from logger import get_logger
import worker

# Create logger with buffer enabled
logger = get_logger(use_buffer=True)

logger.info("Hello buffered logs!")
logger.warning("This is a warning")
logger.error("Error occurred")

worker.do_work()

# Retrieve buffer
for handler in logger.handlers:
    if hasattr(handler, "get_logs"):
        print("Buffered logs:", handler.get_logs())

print(dir(logger))

