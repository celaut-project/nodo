import sys
from subprocess import run
import json
import os
from compile import calculate_service_hash
import gateway_pb2

import logging
logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s')
LOGGER = lambda message: logging.getLogger(__name__).debug(message)

def verify_filesys():
    pass

def build(service):
    id = calculate_service_hash(service.SerializeFromString(), "SHA3_256")
    # Add Entrypoint.
    run('mkdir /home/hy/node/__hycach__/'+id, shell=True)
    with open('/home/hy/node/__hycache__/'+id+'/Dockerfile', 'w') as file:
        with open('/home/hy/node/__registry__/'+id+'/Dockerfile', 'r') as df:
            data = df.read()
        file.write( data + '\nENTRYPOINT '+service.container.entrypoint)

    # build service container.
    run('/usr/bin/docker build -t '+id+'.oci /home/hy/node/__hycache__/'+id+'/.', shell=True)
    verify_filesys()

if __name__ == "__main__":
    id = sys.argv[1]
    with open("/home/hy/node/__registry__/"+id+"/"+id+".service", "rb") as file:
        file = gateway_pb2.ServiceFile()
        file.ParseFromString(file.read())
        if calculate_service_hash(file.service, "SHA3_256") == id:
            build(file.service)
        else:
            print('Error: asignacion de servicio erronea en el registro.')