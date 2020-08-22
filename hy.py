import requests
import sys

image = sys.argv[1]
gateway_host = '127.0.0.1:8080'

print('Init '+image+' image\n-------------\n')

response = requests.get('http://'+'localhost'+'/'+image)
print(response.json)

#!/bin/bash

# Timestamp Function
timestamp() {
    date +"%T"
}

echo "⏲️    $(timestamp): started build script..."
GIT=$(which git)
echo "Buscando GIT  ---->  $GIT"
DOCKER=$(which docker)
echo "Buscando DOCKER  ---->  $DOCKER"
echo "Software necesario listo."