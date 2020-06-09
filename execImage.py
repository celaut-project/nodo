from flask import Flask

if __name__ == "__main__":

    app = Flask(__name__)

    @app.route('/<image>')
    def get(self, name):
        if name=='3723c39d43fc':
            return 'http://0.0.0.0:8000'
        else:
            return 404

    @app.route('/delete/<port_uri>')
    def delete(self, name):
        return 404
 
    app.run(host='0.0.0.0', port=8080)