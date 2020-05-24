
class Pod:
    From = None
    Pkg = []
    def __init__(self):
        super().__init__()
    
    def setFrom(self, line):
        self.From = line

    def setPkg(self, line):
        self.Pkg = line
    
    def show(self):
        print(self.From)
        print(self.Pkg)
    


if __name__ == "__main__":

    def switch(line, pod):
        s = line[0]
        if s == 'FROM':
            pod.setFrom(line[1])
        elif s == 'PKG':
            pod.setPkg(line[1:])

    
    
    
    
    file = open("hyperfile.hy", "r")
    pod = Pod()
    for l in file.readlines():
        switch( l.split(), pod )
    
    pod.show()