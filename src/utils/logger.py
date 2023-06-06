import logging

from src.utils.env import GET_ENV, MAIN_DIR

USE_PRINT = GET_ENV(env='USE_PRINT', default=False)

logging.basicConfig(filename=f'{MAIN_DIR}/app.log', level=logging.INFO,
                    format='%(asctime)s %(levelname)-8s %(message)s')
LOGGER = (lambda message: print(message + '\n')) if USE_PRINT \
    else (lambda message: logging.getLogger(__name__).info(message + '\n'))
