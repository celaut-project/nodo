
from subprocess import run
import json

class Image:
    image = None
    isAbstract = None
    def __init__(self, image):
        self.isAbstract = True
        self.image = image

    @staticmethod
    def makePod(filename):
        def get_this_file():
            pass
        def switch(line, pod):
            s = line[0]
            if s.startswith('.PKG'):
                pod.setPkg(get_this_file())
            elif s == 'API':
                pod.setApi(line[1:])
            elif s == 'CTR':
                pod.setCtr(line[1:])
            elif s == 'TNS':
                pod.setTensor(line[1:])
        file = open(filename, "r")
        image = Image()
        for l in file.readlines():
            try:
                switch( l.split(), image )
            except IndexError:
                break
        return image

    @staticmethod
    def makeImage(filename):
        file = json.load(open(filename,"r"))
        return Image(file)

    def show(self):
        print(self.image)

    def build(self):
        pass
            

def isValidHyperFile(file):
    def isValidBuild():
        pass
    return True

if __name__ == "__main__":
    file="frontier/Hyperfile.json"
    if isValidHyperFile(file):
        image = Image.makePod(file)
        image.show()
        #image.build()