
from subprocess import run
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
        print(self.image.get('BUILD'))

    def build(self):
        def dockerfile():
            myfile = open("Dockerfile", 'w')
            myfile.write(self.image.get('BUILD'))
            myfile.close()
        dockerfile()
        run('docker build .')
        run('rm Dockerfile')
        

def isValidHyperFile(file):
    def isValidBuild():
        pass
    return True

if __name__ == "__main__":
    file="frontier/Hyperfile.json"
    if isValidHyperFile(file):
        image = Image.makeImage(file)
        image.show()
        image.build()