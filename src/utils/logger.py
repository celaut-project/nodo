import logging, os

from src.utils.env import GET_ENV, STORAGE

USE_PRINT = GET_ENV(env='USE_PRINT', default=True)
if not os.path.exists(STORAGE): os.makedirs(STORAGE)

logging.basicConfig(filename=f'{STORAGE}/app.log', level=logging.INFO,
                    format='%(asctime)s %(levelname)-8s %(message)s')
LOGGER = (lambda message: print(message + '\n')) if USE_PRINT \
    else (lambda message: logging.getLogger(__name__).info(message + '\n'))
