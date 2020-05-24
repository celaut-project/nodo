


def makePod(filename):
    def dofrom():
        print("IS FROM")
    def dopkg():
        print("IS PKG")

    def switch(s):
        if s == 'FROM':
            dofrom()
        elif s == 'PKG':
            dopkg()
        else:
            print("OPCION INVALIDA  ",s)

    file = open(filename, "r")
    for l in file.readlines():
        line = l.split(" ")
        switch(line[0])


if __name__ == "__main__":
    makePod("hyperfile.hy")