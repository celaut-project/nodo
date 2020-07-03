from flask import Flask
import json
import buildImage
import subprocess, os

if __name__ == "__main__":

    app = Flask(__name__)

    token_cache = {}

    instance_cache = {}

    @app.route('/')
    def hello():
        return 'HELLO HYPER'

    @app.route('/<dependency>')
    def get(dependency):
        api_port = buildImage.ok(str(dependency)) # Si no esta construido, lo construye.
        container_id = subprocess.check_output('docker run --detach '+dependency+'.oci') # Ejecuta una instancia de la imagen.
        token_cache.update({container_id:instance_cache.get(''''father_uri'''')})
        container_ip = subprocess.check_output("docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "+container_id)
        if api_port == None:
            instance_cache.update({container_ip:container_id}) # uri : token
            return {
                'uri': None,
                'token': container_id
            }
        else:
            instance_cache.update({container_ip:container_id}) # uri : token
            return {
                'uri': container_ip + api_port,
                'token': container_id
            }

    @app.route('/<token>')
    # Se puede usar una cache para la recursividad,
    #  para no estar buscando todo el tiempo lo mismo.
    def delete(token):
        subprocess.check_output('docker rm '+token+' --force')
        for d in token_cache:
            if token_cache.get(d) == token:
                delete(d)
                del token_cache[d]

    app.run(host='0.0.0.0', port=8080)