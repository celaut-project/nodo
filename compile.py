import sys
import json
from subprocess import run, check_output
import os
import hashlib

SHAKE = lambda value: "" if value is None else hashlib.shake_256(value.encode()).hexdigest(256)

# ALERT: Its not async.
SHAKE_STREAM = lambda value: "" if value is None else hashlib.shake_256(value.encode()).hexdigest(99999999)

class Hyper:
    def __init__(self, path, file={
                "Api": None,        # list
                "Container" : None, # dict
                "Contract": None,   # list
                "Dependency": None, # list
                "Ledger": None, 
                "Tensor": None,
            }):
        super().__init__()
        self.file = file
        self.path = path

    def parseDependency(self):
        dependencies = []
        if os.path.isdir(self.path+'dependencies'):
            for file in os.listdir(self.path+'dependencies'):
                image = json.load(open(self.path+'dependencies/'+file,'r'))
                dependencies.append(image)
            if len(dependencies)>0:
                self.file.update({'Dependency':dependencies})

    def parseContainer(self):
        def parseFilesys(container):
            os.system("mkdir /home/node/__hycache__/building")
            if os.path.isfile(self.path+'Dockerfile'):
                os.system('docker build -t building '+self.path)
                os.system("docker save building | gzip > /home/node/__hycache__/building/building.tar.gz")
            elif os.path.isfile(self.path+'building.tar.gz'):
                os.system("mv "+self.path+"building.tar.gz __hycache__/building/")
            else:
                print("Error: Dockerfile o building.tar.gz no encontrados.")
            os.system("cd /home/node/__hycache__/building && tar -xvf building.tar.gz")
            dirs = [] # lista de todos los directorios para ordenar.
            layers = []
            for layer in os.listdir("/home/node/__hycache__/building/"):
                if os.path.isdir("/home/node/__hycache__/building/"+layer):
                    layers.append(layer)
                    print("Layer --> ",layer) # Si accedemos directamente, en vez de descomprimir, será bastante mas rapido.
                    for dir in check_output("cd /home/node/__hycache__/building/"+layer+" && tar -xvf layer.tar", shell=True).decode('utf-8').split("\n")[:-1]:
                        if dir.split(' ')[0]=='/' or len(dir)==1:
                            print("Ghost directory --> "+dir)
                            continue # Estos no se de donde salen.
                        dirs.append(dir)
            def create_tree(index, dirs, layers):
                def add_file(adir, layers):
                    for layer in layers:
                        if os.path.exists('/home/node/__hycache__/building/'+layer+'/'+adir):
                            if os.path.isfile('/home/node/__hycache__/building/'+layer+'/'+adir):
                                if adir == '.wh..wh..opq':
                                    return None
                                print("Archivo --> "+adir)
                                cdir = '/home/node/__hycache__/building/'+layer+'/'+adir
                                try:
                                    info = open(cdir,"r").read()
                                except UnicodeDecodeError: 
                                    info = open(cdir,"br").read().decode('cp437')
                                except FileNotFoundError:
                                    print(adir+" posiblemente vacio.")
                                    exit()
                                except UnboundLocalError:
                                    print("UnboundError")
                                    print(cdir)
                                    exit()
                                return {
                                    "Dir":adir,
                                    "Id":SHAKE(info)
                                }
                            elif os.path.islink('/home/node/__hycache__/building/'+layer+'/'+adir):
                                link = check_output('ls -l /home/node/__hycache__/building/'+layer+'/'+adir, shell=True).decode('utf-8').split(" ")[-1]
                                print("Link --> "+adir)
                                return {
                                    "Dir":adir,
                                    "Id":SHAKE(link)
                                }
                            else:
                                print("ERROR: No deberiamos haber llegado aqui.")
                                os.system("rm -rf /home/node/__hycache__/building")
                                os.system("docker rmi /home/node/__hycache__/building")
                                exit()
                            """elif os.path.ismount('__hycache__/building/'+layer+'/'+adir):
                                print("Mount --> "+adir)
                                print("ERROR: No deberiamos haber llegado aqui.")
                                os.system("rm -rf __hycache__/building")
                                os.system("docker rmi __hycache__/building")
                                exit()
                            elif os.path.isabs('__hycache__/building/'+layer+'/'+adir):
                                print("Abs -->"+adir)
                                print("ERROR: No deberiamos haber llegado aqui.")
                                os.system("rm -rf __hycache__/building")
                                os.system("docker rmi __hycache__/building")
                                exit()"""
                    print("Algo fue mal. No se encontro en ninguna capa ¿?")
                print("           Nueva vuelta",index)
                local_dirs={}
                local_files={}
                for dir in dirs:
                    print("Directory --> "+dir)
                    raiz = dir.split('/')[index]
                    print("Raiz --> "+raiz)
                    if dir[-1]!="/" and dir.split('/')[-1]==raiz:
                        d = add_file(adir=dir, layers=layers)
                        if d is not None:
                            local_files.update({raiz:d})
                    else:
                        if (raiz in local_dirs) == False:
                            print('   Nueva raiz --> '+raiz)  
                            local_dirs.update({raiz:[]})
                        if dir[-1]!="/" or raiz!=dir.split('/')[-2]: # No introducimos usr/ si la raiz es usr.
                            lista = local_dirs[raiz]
                            lista.append(dir)
                            local_dirs.update({raiz:lista})
                for raiz in local_dirs:
                    local_dirs.update({raiz:create_tree(index=index+1,dirs=local_dirs[raiz], layers=layers)})
                return {**local_dirs, **local_files}
            fs_tree = create_tree(index=0,dirs=dirs, layers=layers)
            def make_hash(merkle):
                s=None
                for i in merkle:
                    if s == None: s = i.get('Id')
                    s = s+' '+i.get('Id')
                return SHAKE(s)
            def reorder_tree(tree):
                l = []
                for d in sorted(tree.items(), key=lambda x: x[0]):
                    v = d[1]
                    try:
                        if ('Dir' in v and 'Id' in v)==False: # No es un directorio ...
                            merkle = reorder_tree(tree=v)
                            if merkle == []: continue
                            id = make_hash(merkle=merkle)
                            l.append({
                                'Id' : id,
                                'Merkle': merkle
                                })
                        else:
                            l.append(v)
                    except AttributeError:
                        print(v)
                        exit()
                return l
            fs_tree = reorder_tree(fs_tree)
            fs_tree = {
                "Id": make_hash(fs_tree),
                "Merkle": fs_tree
            }
            container.update({'Filesys':fs_tree})
            return container
        container = self.file.get('Container')
        if container == None:
            container = {
                "Entrypoint" : None,    # string
                "Filesys" : None,        # Merkle
                "Arch" : None,          # list
                "Envs": None          # list
            }
        arch = json.load(open(self.path+"Arch.json", "r"))
        container.update({'Arch' : arch})
        if os.path.isfile(self.path+"Envs.json"):
            envs = json.load(open(self.path+"Envs.json", "r"))
            container.update({'Envs' : envs})
        if os.path.isfile(self.path+"Entrypoint.json"):
            entrypoint = json.load(open(self.path+"Entrypoint.json", "r"))
            container.update({'Entrypoint' : entrypoint})
        container = parseFilesys(container=container)
        self.file.update({'Container' : container})

    def parseApi(self):
        if os.path.isfile(self.path+"Api.json"):
            api = json.load(open(self.path+"Api.json", "r"))
            self.file.update({'Api' : api})


    def parseLedger(self):
        pass

    def parseTensor(self):
        pass
    
    @staticmethod
    def getId(hyperfile):
        info = json.dumps(hyperfile)
        print(info)
        return SHAKE(info)

    def save(self):
        id = Hyper.getId(hyperfile=self.file) 
        file_dir = '/home/node/__registry__/' +id+ '.json'
        with open(file_dir,'w') as f:
            f.write( json.dumps(self.file) )
        os.system('mkdir /home/node/__registry__/'+id)
        os.system('mv '+self.path+' home/node/__registry__/'+id+'/')

def ok(path):
    Hyperfile = Hyper(path=path)

    Hyperfile.parseContainer()
    Hyperfile.parseApi()
    Hyperfile.parseDependency()
    Hyperfile.parseLedger()
    Hyperfile.parseTensor()

    Hyperfile.save()

if __name__ == "__main__":
    if len(sys.argv) == 1:
        ok(path='/home/node/__hycache__/for_build/')  # Hyperfile
    elif len(sys.argv) == 2:
        git = str(sys.argv[1])
        repo = git.split('::')[0]
        branch = git.split('::')[1]
        os.system('git clone --branch '+branch+' '+repo+' /home/node/__hycache__/for_build/git')
        print(os.listdir('/home/node/__hycache__/for_build/git/.hy/'))
        ok(path='/home/node/__hycache__/for_build/git/.hy/')  # Hyperfile
    else:
        print('NO SE ACPTAN MAS PARÁMETROS..')

    if os.path.isfile('__hycache__/for_build/Arch.json') == False:
        print('ForBuild invalido, Arch.json OBLIGATORIOS ....')
        exit()
