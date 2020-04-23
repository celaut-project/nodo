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