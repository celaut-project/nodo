
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
    
    @staticmethod
    def makePod(filename):
        def get_this_file(s):
            pass
        def switch(line, pod):
            s = line[0]
            if s.startswith('PKG'):
                pod.setPkg(s[3], get_this_file(line[1]))
            elif s == 'API':
                pod.setApi(line[1:])
            elif s == 'CTR':
                pod.setCtr(line[1:])
            elif s == 'TNS':
                pod.setTensor(line[1:])
        file = open(filename, "r")
        pod = Pod()
        for l in file.readlines():
            try:
                switch( l.split(), pod )
            except IndexError:
                break
        return pod

    def build(self):
        for k in self.Pkgs.keys():
            line = self.Pkgs.get(k)
            

def isValidHyperFile(file):
    return True

if __name__ == "__main__":
    file="hyperfiles/frontier.hy"
    if isValidHyperFile(file):
        pod = Pod.makePod(file)
        #pod.show()
        pod.build()