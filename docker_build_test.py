import os
from random import randint
from subprocess import check_output, run
import celaut_pb2, gateway_pb2

def write_item(b: celaut_pb2.Service.Container.Filesystem.ItemBranch, dir: str, symlinks):
    if b.HasField('filesystem'):
        os.mkdir(dir + b.name)
        write_fs(fs = b.filesystem, dir = dir + b.name + '/', symlinks = symlinks)

    elif b.HasField('file'):
        open(dir + b.name, 'wb').write(
            b.file
        )
        
    else:
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
    open('__registry__/249c3e15935a2ce77fb695067224c95e95338ca7be9f4f1b2eb076c2b0c515a3', 'rb').read()
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
check_output('docker build -t '+id+' '+dir+'/.', shell=True)

# Generate the symlinks.
for symlink in symlinks:
    overlay_dir = check_output("docker inspect --format='{{ .GraphDriver.Data.UpperDir }}' "+id, shell=True).decode('utf-8')[:-1]
    run('ln -s '+symlink.src[1:]+' '+symlink.dst[1:], shell=True, cwd=overlay_dir)

# Apply permissions.