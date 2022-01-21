import os
from random import randint
from subprocess import check_output
import celaut_pb2, gateway_pb2

def write_item(b: celaut_pb2.Service.Container.Filesystem.ItemBranch, dir: str, root_dir: str):
    if b.HasField('filesystem'):
        os.mkdir(dir + b.name)
        write_fs(fs = b.filesystem, dir = dir + b.name + '/', root_dir = root_dir)

    elif b.HasField('file'):
        open(dir + b.name, 'wb').write(
            b.file
        )
        
    else:
        # TODO que pasa si la carpeta dst. aun no se a escrito??
        print('dir -> ', root_dir)
        print('src -> ', b.link.src)
        print('dst -> ', b.link.dst)
        os.symlink(
            src = root_dir + b.link.src,
            dst = root_dir + b.link.dst
        )

def write_fs(fs: celaut_pb2.Service.Container.Filesystem, dir: str, root_dir: str):
    for branch in fs.branch:
        write_item(
            b = branch,
            dir = dir,
            root_dir = root_dir
        )

service_with_meta = gateway_pb2.ServiceWithMeta()
service_with_meta.ParseFromString(
    open('__registry__/bc331dfbda2807aba247bfceaad75a8751d482654a3a7ac7256c74ef5b012b01', 'rb').read()
)

fs = celaut_pb2.Service.Container.Filesystem()
fs.ParseFromString(
    service_with_meta.service.container.filesystem
)

try:
    os.mkdir('__hycache__')
except: pass

id = str(randint(1,999))
dir = '__hycache__/builder'+id
os.mkdir(dir)
fs_dir = dir + '/fs'
os.mkdir(fs_dir)
write_fs(fs = fs, dir = fs_dir + '/', root_dir = fs_dir)

open(dir+'/Dockerfile', 'w').write('FROM scratch\nCOPY fs .\nENTRYPOINT /random/start.py')
check_output('docker build -t '+id+' '+dir+'/.', shell=True)