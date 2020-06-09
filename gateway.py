from flask import Flask
import buildImage
from subprocess import run

if __name__ == "__main__":

    app = Flask(__name__)

    @app.route('/<image>')
    def get(image):
        container_id = buildImage.ok(str(image)) # Si no esta construido, lo construye.
        print(container_id)
        pod_port = buildImage.select_port()
        #run('docker exec ',container_id,' --port ',pod_port) # Ejecuta una instancia de la imagen con el puerto que sea.
        pod_api = '/'
        return 'http://0.0.0.0:'+pod_port+pod_api

    @app.route('/delete/<port_uri>')
    def delete(port_uri):
        return 404

    app.run(host='0.0.0.0', port=8080)