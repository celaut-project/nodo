import logging, os
GET_ENV = lambda env, default: ( type(default)(os.environ.get(env)) if type(default)!=bool else os.environ.get(env) in ['True', 'true', 'T', 't'] ) if env in os.environ.keys() else default
USE_PRINT = GET_ENV(env = 'USE_PRINT', default = False)

logging.basicConfig(filename='/node/app.log', level=logging.INFO, format='%(asctime)s %(levelname)-8s %(message)s')
LOGGER = (lambda message: print(message + '\n')) if USE_PRINT \
     else (lambda message: logging.getLogger(__name__).info(message + '\n'));     LOGGER('USE PRINT -> '+ str(USE_PRINT))