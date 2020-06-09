import sys
from subprocess import run
import os
import json

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
            myfile.write(self.image.get('Build'))
            myfile.close()
        dockerfile()
        run('docker build .')
        run('rm Dockerfile')

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
    return '8000'

def ok(image):
    file =  os.listdir('registry/'+image+'.json')
    container_id = main(file).image.get('Container').get('Id')
    container_id = container_id.split(':')[1]
    return container_id

if __name__ == "__main__":
    file=sys.argv[1]
    main(file)