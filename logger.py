import logging, getpass
logging.basicConfig(filename='/home/'+getpass.getuser()+'/node/app.log', level=logging.DEBUG, format='%(asctime)s %(levelname)-8s %(message)s')
#LOGGER = lambda message: logging.getLogger(__name__).debug(message + '\n')
LOGGER = lambda message: print(message + '\n')