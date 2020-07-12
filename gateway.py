from flask import Flask, request
import json
import build
import subprocess, os

if __name__ == "__main__":

    app = Flask(__name__)

    token_cache = {} # token : token del padre  ( si es tokenhoster es que no tiene padre..)

    instance_cache = {}  # uri : token

    def dependency(dependency):
        api_port = build.ok(str(dependency)) # Si no esta construido, lo construye.
        
        envs = request.json
        if envs == None:
            container_id = subprocess.check_output('docker run --detach '+dependency+'.oci').decode('utf-8') # Ejecuta una instancia de la imagen.
        else:
            command = 'docker run'
            for env in envs:
                command = command +' -e "'+env+'='+envs[env]+'"'
            command = command + ' --detach '+dependency+'.oci'
            container_id = subprocess.check_output(command).decode('utf-8')

        if request.remote_addr in instance_cache.keys():
            father_token = instance_cache.get(request.remote_addr)
        else:
            father_token = 'tokenhoster'
            instance_cache.update({request.remote_addr:'tokenhoster'})
        token_cache.update({container_id:father_token})
        container_ip = subprocess.check_output("docker inspect --format \"{{ .NetworkSettings.IPAddress }}\" "+container_id ).decode('utf-8')
        if api_port == None:
            instance_cache.update({container_ip:container_id})
            return {
                'uri': None,
                'token': container_id
            }
        else:
            instance_cache.update({container_ip:container_id})
            return {
                'uri': container_ip + api_port,
                'token': container_id
            }

    # Se puede usar una cache para la recursividad,
    #  para no estar buscando todo el tiempo lo mismo.
    def token(token):
        if token == 'tokenhoster':
            print('TOKENHOSTER NO SE TOCA')
        else:
            subprocess.check_output('docker rm '+token+' --force')
            for d in token_cache:
                if token_cache.get(d) == token:
                    token(d)
                    del token_cache[d]  

    @app.route('/<hello>',  methods=['GET', 'POST'])
    def hello(hello):
        if len(hello)==64:
            dependency(hello)
        else:
            token(hello)

    app.run(host='0.0.0.0', port=8080)