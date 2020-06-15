from flask import Flask
import buildImage
from subprocess import run

if __name__ == "__main__":

    app = Flask(__name__)

    @app.route('/<image>')
    def get(image):
        pod_id, api_port = buildImage.ok(str(image)) # Si no esta construido, lo construye.
        pod_port = buildImage.select_port()
        run('docker run -p '+pod_port+':'+api_port+" "+pod_id+'.oci') # Ejecuta una instancia de la imagen con el puerto que sea.
        return 'http://0.0.0.0:'+pod_port

    @app.route('/delete/<port_uri>')
    def delete(port_uri):
        return 404

    app.run(host='0.0.0.0', port=8080)