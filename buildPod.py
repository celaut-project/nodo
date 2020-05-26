
from subprocess import run

class Pod:
    Pkgs = {}       
    Api = None
    Tensor = None
    Contract = None
    isAbstract = None
    def __init__(self):
        super().__init__()
        self.isAbstract = True

    def setPkg(self,key,line):
        self.Pkgs.update({key:line})

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
    
    def build(self):
        pass



def makePod(filename):
    def switch(line, pod):
        print(line)
        s = line[0]
        if s.split()[:3] == 'PKG':
            pod.setPkg(s.split()[3],line[1:])
        elif s == 'API':
            pod.setApi(line[1:])
        elif s == 'CTR':
            pod.setCtr(line[1:])
        elif s == 'TNS':
            pod.setTensor(line[1:])
    file = open(filename, "r")
    pod = Pod()
    for l in file.readlines():
        while l!='':
            switch( l.split(), pod )
    return pod

def isValidHyperFile(file):
    return True

if __name__ == "__main__":
    file="hyperfiles/frontier.hy"
    if isValidHyperFile(file):
        pod = makePod(file)
        pod.show()
        pod.build()