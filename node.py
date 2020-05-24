def makePod(filename):
    def dofrom(line):
        print(line)
    def dopkg(line):
        print(line)

    def switch(line):
        s = line[0]
        if s == 'FROM':
            dofrom(line[1])
        elif s == 'PKG':
            dopkg(line[1:])
        else:
            print("      OPCION INVALIDA  ",s)

    file = open(filename, "r")
    for l in file.readlines():
        switch( l.split() )


if __name__ == "__main__":
    makePod("hyperfile.hy")