import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/home/hy/node/app.log"),
        logging.StreamHandler()
    ]
    )
LOGGER = lambda message: logging.getLogger(__name__).debug(message)