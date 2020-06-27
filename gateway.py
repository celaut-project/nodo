from flask import Flask
import buildImage
import os

if __name__ == "__main__":

    app = Flask(__name__)
    gateway_node_port = 8080

    @app.route('/')
    def hello():
        return 'HELLO HYPER'

    @app.route('/<image>')
    def get(image):
        api_port, gateway_port = buildImage.ok(str(image)) # Si no esta construido, lo construye.
        pod_port = buildImage.select_port()
        os.system('docker run --detach -p 127.0.0.1:'+pod_port+':'+api_port+' -p 127.0.0.1:'+gateway_node_port+':'+gateway_port+' '+image+'.oci') # Ejecuta una instancia de la imagen con el puerto que sea.

    @app.route('/<port_uri>')
    def delete(port_uri):
        pass

    app.run(host='0.0.0.0', port=8080)