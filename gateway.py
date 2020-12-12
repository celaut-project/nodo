from flask import Flask, request, jsonify, abort
import json
import build
import subprocess, os

import logging
logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s')
LOGGER = lambda message: logging.getLogger(__name__).debug(message)

app = Flask(__name__)

token_cache = {} # token : token del padre  ( si es tokenhoster es que no tiene padre..)

instance_cache = {}  # uri : token

def dependency(dependency):
    try:
        api_port = build.ok(str(dependency)) # Si no esta construido, lo construye.
        LOGGER(str(api_port))
    except build.ImageException as e:
        LOGGER('Salta la excepcion '+str(e))

    if api_port == None:
        LOGGER('No retorna direccion, no hay api.')

        envs = request.json.get('envs') or None # TODO adaptative_api
        if envs == None:
            container_id = subprocess.check_output('docker run --detach '+dependency+'.oci', shell=True).decode('utf-8').replace('\n', '') # Ejecuta una instancia de la imagen.
        else:
            command = 'docker run'
            for env in envs:
                command = command +' -e '+env+'='+envs[env]
            command = command +' --detach '+dependency+'.oci'
            container_id = subprocess.check_output(command, shell=True).decode('utf-8').replace('\n', '')

        if request.remote_addr in instance_cache.keys():
            father_token = instance_cache.get(request.remote_addr)
        else:
            father_token = 'tokenhoster'
            instance_cache.update({request.remote_addr:'tokenhoster'})
        token_cache.update({container_id:father_token})
        while 1:
            try:
                container_ip = subprocess.check_output("docker inspect --format \"{{ .NetworkSettings.IPAddress }}\" "+container_id , shell=True).decode('utf-8').replace('\n', '')
                LOGGER(str(container_ip))
                break
            except subprocess.CalledProcessError as e:
                LOGGER(e.output)


        instance_cache.update({container_ip:container_id})
        return jsonify( {
            'uri': None,
            'token': container_id
        } )

    else:
        LOGGER('Retorna la uri para usar la api.'+ str(api_port))

        if (request.remote_addr)[:7] == '172.17.' or (request.remote_addr) == '127.0.0.1':
            envs = request.json
            if envs == None:
                container_id = subprocess.check_output('docker run --detach '+dependency+'.oci', shell=True).decode('utf-8').replace('\n', '') # Ejecuta una instancia de la imagen.
            else:
                command = 'docker run'
                for env in envs:
                    command = command +' -e "'+env+'='+envs[env]+'"'
                command = command +' --detach '+dependency+'.oci'
                container_id = subprocess.check_output(command, shell=True).decode('utf-8').replace('\n', '')

            if request.remote_addr in instance_cache.keys():
                father_token = instance_cache.get(request.remote_addr)
            else:
                father_token = 'tokenhoster'
                instance_cache.update({request.remote_addr:'tokenhoster'})
            token_cache.update({container_id:father_token})
            while 1:
                try:
                    container_ip = subprocess.check_output("docker inspect --format \"{{ .NetworkSettings.IPAddress }}\" "+container_id , shell=True).decode('utf-8').replace('\n', '')
                    LOGGER(container_ip)
                    break
                except subprocess.CalledProcessError as e:
                    LOGGER(e.output)

            instance_cache.update({container_ip:container_id})
            return jsonify( {
                'uri': container_ip + ':' + api_port,
                'token': container_id
            } )
        elif (request.remote_addr)[:4] == '192.':
            def get_host_ip():
                import socket as s
                return s.gethostbyname( s.gethostname() )
            def get_free_port():
                from socket import socket
                with socket() as s:
                    s.bind(('',0))
                    return str(s.getsockname()[1])
            host_ip = get_host_ip()
            free_port = get_free_port()
            command = 'docker run -p '+free_port+':'+api_port+' --detach'
            if envs:
                for env in envs:
                    command = command +' -e "'+env+'='+envs[env]+'"'
            command = command+' '+dependency+'.oci'
            container_id = subprocess.check_output(command, shell=True).decode('utf-8').replace('\n', '')

            if request.remote_addr in instance_cache.keys():
                father_token = instance_cache.get(request.remote_addr)
            else:
                father_token = 'tokenhoster'
                instance_cache.update({request.remote_addr:'tokenhoster'})
            token_cache.update({container_id:father_token})
            while 1:
                try:
                    container_ip = subprocess.check_output("docker inspect --format \"{{ .NetworkSettings.IPAddress }}\" "+container_id , shell=True).decode('utf-8').replace('\n', '')
                    LOGGER(str(container_ip))
                    break
                except subprocess.CalledProcessError as e:
                    LOGGER(e.output)

            instance_cache.update({container_ip:container_id})
            return jsonify( {
                'uri': host_ip + ':' + free_port,
                'token': container_id
            } )
        else:
            abort('400','THIS NETWORK IS NOT SUPPORTED')

# Se puede usar una cache para la recursividad,
#  para no estar buscando todo el tiempo lo mismo.
def token(token):
    if token == 'tokenhoster':
        LOGGER('TOKENHOSTER NO SE TOCA')
    else:
        subprocess.check_output('docker rm '+token+' --force', shell=True)
        for d in token_cache:
            if token_cache.get(d) == token:
                token(d)
                del token_cache[d]

def node_list():
    response = {}
    for query_service in os.listdir('__nodes__'):
        response.update({
            'uri': query_service.uri,
            'json': query_service.json
        })
    return response

@app.route('/',  methods=['POST', 'GET', 'PUT'])
def hello():
    servicio = request.json.get('service') or None # TODO adaptative_api
    if servicio is None:
        token = request.json.get('token')
        return token if token is not None else 'HY.'
    if type(servicio) is not str:
        LOGGER('HY '+request.remote_addr)
        return 'HY.'
    if servicio=='@?':
        return node_list()
    elif servicio == '@!' or servicio == 'HEY':
        return 'HY.'
    else:
        return dependency(servicio)
