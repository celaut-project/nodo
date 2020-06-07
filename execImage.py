from flask import Flask
from flask_restful import Resource, Api

if __name__ == "__main__":
    app = Flask(__name__)
    api = Api(app)

    class Get(Resource):
        def get(self, name):
            return {"image":name}
    
    api.add_resource(Get, "/")
    app.run(host='0.0.0.0', port=8080)