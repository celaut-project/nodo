
from subprocess import run

class Pod:
    From = None         # Apunta a otro Hyperfile.
    Pkgs = []   # Para diferenciar entre PKG y RUN, usar tuplas, si usamos dos listas distintas no sabremos el orden.
    Api = None
    Tensor = None
    def __init__(self):
        super().__init__()
    
    def setFrom(self, line):
        self.From = line

    def setPkg(self, line):
        self.Pkgs.append(line)

    def setApi(self, line):
        self.Api = line

    def setCtr(self, line):
        self.Contract = line

    def setTensor(self, line):
        self.Tensor = line
    
    def show(self):
        print(self.From)
        print(self.Api)
        print(self.Contract)
        print(self.Tensor)
        print(self.Pkgs)
    
    def build(self):
        def isHyper(filename):
            pass
        def isOCI(filename):
            pass
        def isDocker(filename):
            pass
        if isHyper(self.From):
            abstract_pod = makePod(self.From)
            abstract_pod.build()
        elif isOCI(self.From):
            pass
        elif isDocker(self.From):
            run("docker build ",self.From)




def makePod(filename):

    def switch(line, pod):
        s = line[0]
        if s == 'FROM':
            pod.setFrom(line[1])
        elif s == 'PKG':
            pod.setIns(line[1:])
        elif s == 'API':
            pod.setApi(line[1:])
        elif s == 'CTR':
            pod.setCtr(line[1:])
        elif s == 'TNS':
            pod.setTensor(line[1:])




    
    file = open(filename, "r")
    pod = Pod()
    for l in file.readlines():
        switch( l.split(), pod )
    
    pod.show()
    pod.build()

if __name__ == "__main__":
    makePod("hyperfile.hy")