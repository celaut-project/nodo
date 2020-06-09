import sys
from subprocess import run
import json
import os

class Image:
    image = None
    isAbstract = None
    def __init__(self, image):
        self.isAbstract = True
        self.image = image

    @staticmethod
    def makeImage(filename):
        file = json.load(open(filename,"r"))
        return Image(file)

    def show(self):
        print(self.image)

    def build(self):
        def dockerfile():
            myfile = open("Dockerfile", 'w')
            myfile.write(self.image.get('Container').get('Build'))
            myfile.close()
        dockerfile()
        run('docker build .')
        os.remove("Dockerfile")

def isValidHyperFile(file):
    def isValidBuild():
        pass
    return True

def main(file):
    if isValidHyperFile(file):
        image = Image.makeImage(file)
        image.show()
        image.build() 
        return image   

def select_port():
    import socket
    from contextlib import closing
    def find_free_port():
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.bind(('', 0))
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return s.getsockname()[1]
    return find_free_port()

def ok(image):
    file =  "registry/"+image+".json"
    container_id = main(file).image.get('Container').get('Id')
    container_id = container_id.split(':')[1]
    return container_id

if __name__ == "__main__":
    file=sys.argv[1]
    main(file)