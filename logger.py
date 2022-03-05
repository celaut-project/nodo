import logging, utils

USE_PRINT = utils.GET_ENV(env = 'USE_PRINT', default = False)

logging.basicConfig(filename='/node/app.log', level=logging.DEBUG, format='%(asctime)s %(levelname)-8s %(message)s')
LOGGER = lambda message: print(message + '\n') if USE_PRINT else logging.getLogger(__name__).debug(message + '\n')