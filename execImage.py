from flask import Flask
from flask_restful import Resource, Api

if __name__ == "__main__":
    app = Flask(__name__)
    api = Api(app)

    class Get(Resource):
        def get(self, name):
            return {"image":"3723c39d43fc"}
    
    class Delete(Resource):
        def delete  (self, name):
            return {"image":"3723c39d43fc"}

    api.add_resource(Delete, "/delete")
    api.add_resource(Get, "/get")
    app.run(host='0.0.0.0', port=8080)