import os
from random import randint
from shutil import rmtree
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
    open('__registry__/ba2a1ae598c805782adc94fea8d620e5c7ea1cfabc2eeca0e2d7d6adb4e82d0c', 'rb').read()
)

fs = celaut_pb2.Service.Container.Filesystem()
fs.ParseFromString(
    service_with_meta.service.container.filesystem
)

arch = 'linux/arm64' # get_arch_tag(service_with_meta=service_with_meta) # TODO: get_arch_tag, selecciona el tag de la arquitectura definida por el servicio, en base a la especificacion y metadatos, que tiene el nodo para esa arquitectura.

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
check_output('docker buildx build --platform '+arch+' -t '+id+' '+dir+'/.', shell=True)
try:
    rmtree(dir)
except Exception: pass

# Generate the symlinks.
overlay_dir = check_output("docker inspect --format='{{ .GraphDriver.Data.UpperDir }}' "+id, shell=True).decode('utf-8')[:-1]
for symlink in symlinks:
    run('ln -s '+symlink.src+' '+symlink.dst[1:], shell=True, cwd=overlay_dir)

# Apply permissions.