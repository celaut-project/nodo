from flask import Flask
from flask_restful import Resource, Api

if __name__ == "__main__":
    app = Flask(__name__)
    api = Api(app)

    class Get(Resource):
        def get(self, name):
            if name=='3723c39d43fc':
                return {"uri":'http://0.0.0.0:8000'}
            else:
                return 404

    api.add_resource(Get, "/")  
    app.run(host='0.0.0.0', port=8080)