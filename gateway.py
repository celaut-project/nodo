from flask import Flask
import buildImage
import os

if __name__ == "__main__":

    app = Flask(__name__)

    @app.route('/')
    def hello():
        return 'HELLO HYPER'

    @app.route('/<image>')
    def get(image):
        pod_id, api_port = buildImage.ok(str(image)) # Si no esta construido, lo construye.
        pod_port = buildImage.select_port()
        os.system('docker run --detach -p 127.0.0.1:'+pod_port+':'+api_port+' '+pod_id+'.oci') # Ejecuta una instancia de la imagen con el puerto que sea.
        return 'http://127.0.0.1:'+pod_port+'/'

    @app.route('/delete/<port_uri>')
    def delete(port_uri):
        return 404

    app.run(host='127.0.0.1', port=8080)