import sys
from subprocess import run
import json
import os
from compile import SHAKE

import logging
logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s')
LOGGER = lambda message: logging.getLogger(__name__).debug(message)

class Image:
    def __init__(self, service, id):
        self.service = service
        self.id = id

    def show(self):
        LOGGER(self.service)

    def build(self):
        def verify_filesys():
            pass
        def dependency():
            # Add dependencies on the __registry__.
            for dependency in self.service.dependencie:
                file = json.dumps(dependency)
                id = SHAKE(file)
                if os.path.isfile('/home/hy/node/__registry__/'+id+'.json') is False:
                    with open('/home/hy/node/__registry__/'+id+'.json','w') as f:
                        f.write(file)
        dependency()
        # Add Entrypoint.
        with open('/home/hy/node/__hycache__/Dockerfile', 'w') as file:
            with open('/home/hy/node/__registry__/'+self.id+'/Dockerfile', 'r') as df:
                data = df.read()
            file.write( data + '\nENTRYPOINT '+self.service.container.entrypoint)
        run('/usr/bin/docker build -t '+self.id+'.oci /home/hy/node/__hycache__/.', shell=True)
        verify_filesys()

def isValidHyperFile(service):
    return True

def main(service, id=None):
    if isValidHyperFile(service=service):
        image = Image(service=service, id=id)
        image.build()
        return image

class ImageException(Exception):
    LOGGER(Exception)

if __name__ == "__main__":
    id = sys.argv[1]
    with open("/home/hy/node/__registry__/"+id+".service", "rb") as file:
        service = file.read()
    img = main(service=service, id=id)
    LOGGER(str(img.id))