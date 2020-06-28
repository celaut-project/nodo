import sys
from subprocess import run
import json
import os

class Image:
    image = None
    def __init__(self, image):
        self.image = image
        self.id_value = image.get('Merkle').get('Id').split(':')[1]
        self.api_port = image.get('Api').get('Port')
        self.gateway_port = image.get('Gateway')

    @staticmethod
    def makeImage(filename):
        file = json.load(open(filename,"r"))
        return Image(file)

    def show(self):
        print(self.image.get('Container'))

    def build(self):
        def dependency():
            for dependency in self.image.get('Dependency'):
                id = dependency.get('Image').get('Merkle').get('Id').split(':')[:1]
                ok(id)
        def dockerfile():
            def runs():
                string = ""
                for layer in self.image.get('Container').get('Layers'):
                    # Aqui se puede elegir el elemento de la lista que mejor te venga.
                    build = layer.get('Build')
                    if build is not None: string = string+build[0]+'\n'
                return string
            def entrypoint():
                string = 'ENTRYPOINT '+self.image.get('Container').get('Entrypoint')
                if string is not None: return string +'\n'
                else: return ""
            def workingdir():
                string = 'WORKDIR '+self.image.get('Container').get('WorkingDir')
                if string is not None: return string +'\n'
                else: return ""
            myfile = open("Dockerfile", 'w')
            myfile.write(runs())
            myfile.write(entrypoint())
            myfile.write(workingdir())
            myfile.close()
        dependency()
        dockerfile()
        run('docker build -t '+self.id_value+'.oci .')
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
    return str(find_free_port())

def ok(image):
    file =  "registry/"+image+".json"
    if os.path.isfile(file):
        img = main(file)
        if img.id_value == image:
            return img.api_port, img.gateway_port
        else:
            return 404
    else:
        return 404

if __name__ == "__main__":
    file=sys.argv[1]
    img = main(file)
    print(img.id_value)