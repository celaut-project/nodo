import os
from random import randint
from subprocess import check_output
import celaut_pb2, gateway_pb2

def write_item(b: celaut_pb2.Service.Container.Filesystem.ItemBranch, dir: str, symlinks):
    if b.HasField('filesystem'):
        os.mkdir(dir + b.name)
        write_fs(fs = b.filesystem, dir = dir + b.name + '/')

    elif b.HasField('file'):
        open(dir + b.name, 'wb').write(
            b.file
        )
        
    else:
        print('src -> ', b.link.src)
        print('dst -> ', b.link.dst)
        symlinks.append(b.link)

def write_fs(fs: celaut_pb2.Service.Container.Filesystem, dir: str, symlinks):
    for branch in fs.branch:
        write_item(
            b = branch,
            dir = dir,
            symlinks = symlinks
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
symlinks = []
write_fs(fs = fs, dir = fs_dir + '/', symlinks = symlinks)

open(dir+'/Dockerfile', 'w').write('FROM scratch\nCOPY fs .\nENTRYPOINT /random/start.py')
# TODO add symlinks script

check_output('docker build -t '+id+' '+dir+'/.', shell=True)