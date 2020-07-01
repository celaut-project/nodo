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
        os.system('docker run --detach '+dependency+'.oci') # Ejecuta una instancia de la imagen con el puerto pod_port.
        return {
            'uri': container_ip + api_port,
            'token': container_id
        }


    @app.route('/<token>')
    def delete(token):
        pass

    app.run(host='0.0.0.0', port=8080)