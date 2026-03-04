import logging
import sys

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,  # change to DEBUG in staging
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout)
        ],
    )

def get_logger(name: str):
    return logging.getLogger(name)