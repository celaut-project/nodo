import psutil
from time import sleep
while True:
    print('RAM memory % used:', psutil.virtual_memory()[2])
    sleep(1)