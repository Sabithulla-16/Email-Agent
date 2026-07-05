import logging
import sys

def setup_logger(name: str = "email_agent") -> logging.Logger:
    """Sets up a centralized logger for the application."""
    logger = logging.getLogger(name)
    
    # Prevent adding multiple handlers if called multiple times
    if logger.hasHandlers():
        return logger

    logger.setLevel(logging.INFO)
    
    # Create a console handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    
    # Create a formatter
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    
    logger.addHandler(handler)
    return logger

# Create a default logger instance
logger = setup_logger()