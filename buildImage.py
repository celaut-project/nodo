
from subprocess import run

class Image:
    Pkgs = []       
    Api = None
    Tensor = None
    Contract = None
    isAbstract = None
    def __init__(self):
        super().__init__()
        self.isAbstract = True

    def setPkg(self, file):
        self.Pkgs.append(file)

    def setApi(self, line):
        self.Api = line
        self.isAbstract = False

    def setCtr(self, line):
        self.Contract = line

    def setTensor(self, line):
        self.Tensor = line
    
    def show(self):
        print('API ',self.Api)
        print('Contract',self.Contract)
        print('Tensor ',self.Tensor)
        print('Packages ',self.Pkgs)
        print('Abstract ',self.isAbstract)
    
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

    def build(self):
        OCIfile = None
        run("docker build - < ",OCIfile)
            

def isValidHyperFile(file):
    def isValidBuild():
        pass
    return True

if __name__ == "__main__":
    file="hyperfiles/frontier.hy"
    if isValidHyperFile(file):
        image = Image.makePod(file)
        image.show()
        image.build()