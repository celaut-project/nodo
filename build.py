import sys
from subprocess import run
import json
import os

class Image:
    image = None
    def __init__(self, image):
        self.image = image
        self.id_value = image.get('Merkle').get('Id').split(':')[1]
    
    def api_port(self):
        if self.image.get('Api')== None:
            return None
        else:
            return self.image.get('Api').get('Port')

    @staticmethod
    def makeImage(filename):
        file = json.load(open(filename,"r"))
        return Image(file)

    def show(self):
        print(self.image.get('Container'))

    def build(self):
        def dependency():
            dependencies = self.image.get('Dependency')
            if dependencies != None:
                for dependency in dependencies:
                    id = dependency.get('Merkle').get('Id').split(':')[:1]
                    if os.path.isfile('registry/'+id+'.json') is False:
                        with open(id+'.json','w') as file:
                            file.write( json.dumps(dependency, indent=4, sort_keys=True) )
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
        run('sudo docker build -t '+self.id_value+'.oci .', shell=True)
        os.remove("Dockerfile")

def isValidHyperFile(file):
    def isValidBuild():
        pass
    return True

def main(file):
    if isValidHyperFile(file):
        image = Image.makeImage(file)
        image.build() 
        return image   

class ImageException(Exception):
    print(Exception)

def ok(image):

    file =  "registry/"+image+".json"
    if os.path.isfile(file):
        img = main(file)
        if img.id_value == image:
            api_port = img.api_port()
            print('Retorna el puerto de la API', api_port)
            return api_port
        else:
            raise ImageException('Imagen erronea..')
    else:
        raise ImageException('No se encuentra en el registro ...')

if __name__ == "__main__":
    file=sys.argv[1]
    img = main(file)
    print(img.id_value)