from time import sleep, localtime, strftime
from subprocess import check_output
while True:
    t = localtime()
    current_time = strftime("%H:%M:%S", t)
    print(current_time)
    print(current_time, check_output('netstat -natup | grep 8090'))
    sleep(1)