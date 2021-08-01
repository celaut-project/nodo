import logging
logging.basicConfig(filename='/home/hy/node/app.log', level=logging.DEBUG, format='%(asctime)s %(levelname)-8s %(message)s')
LOGGER = lambda message: logging.getLogger(__name__).debug(message + '\n')