from flask import Flask
import json
import buildImage
import subprocess, os

if __name__ == "__main__":

    app = Flask(__name__)

    @app.route('/')
    def hello():
        return 'HELLO HYPER'

    @app.route('/<dependency>')
    def get(dependency):
        api_port = buildImage.ok(str(dependency)) # Si no esta construido, lo construye.
        container_id = subprocess.check_output('docker run --detach '+dependency+'.oci') # Ejecuta una instancia de la imagen.
        if api_port == None:
            return {
                'uri': None,
                'token': container_id
            }
        else:
            container_ip = subprocess.check_output("docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "+container_id)
            return {
                'uri': container_ip + api_port,
                'token': container_id
            }

    @app.route('/<token>')
    def delete(token):
        subprocess.check_output('docker rm '+token+' --force')
        # delete all dependencies

    app.run(host='0.0.0.0', port=8080)