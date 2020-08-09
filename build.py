import sys
from subprocess import run
import json
import os
from compile import sha256

class Image:
    image = None
    def __init__(self, image, id):
        self.image = image
        self.id = id
    
    def api_port(self):
        if self.image.get('Api')== None:
            return None
        else:
            return self.image.get('Api').get('Port')

    @staticmethod
    def makeImage(filename, id):
        file = json.load(open(filename,"r"))
        return Image(image=file, id=id)

    def show(self):
        print(self.image)

    def build(self):
        def verify_filesys():
            pass
        def dependency():
            # Add dependencies on the registry.
            dependencies = self.image.get('Dependency')
            if dependencies != None:
                for dependency in dependencies:
                    file = json.dumps(dependency)
                    id = sha256(file)
                    if os.path.isfile('registry/'+id+'.json') is False:
                        with open('registry/'+id+'.json','w') as file:
                            file.write(file)
        dependency()
        run('sudo docker build -t '+self.id+'.oci ./registry/'+self.id+'/', shell=True)
        verify_filesys()

def isValidHyperFile(file):
    return True

def main(filename, id):
    if isValidHyperFile(file):
        image = Image.makeImage(filename=file, id=id)
        image.build()
        return image

class ImageException(Exception):
    print(Exception)

def ok(image):

    filename =  "registry/"+image+".json"
    if os.path.isfile(filename):
        img = main(filename=filename, id=image)
        if img.id == image:
            api_port = img.api_port()
            print('Retorna el puerto de la API', api_port)
            return api_port
        else:
            raise ImageException('Imagen erronea..')
    else:
        raise ImageException('No se encuentra en el registro ...')

if __name__ == "__main__":
    image = sys.argv[1]
    file =  "registry/"+image+".json"
    img = main(filename=file, id=image)
    print(img.id)
