from flask import Flask
from flask_restful import Resource, Api

if __name__ == "__main__":
    app = Flask(__name__)
    api = Api(app)

    class Get(Resource):
        def get(self, name):
            return {"image":"2183y4h3lnj1"}
    
    class Delete(Resource):
        def delete  (self, name):
            return {"image":"2183y4h3lnj1"}

    api.add_resource(Delete, "/delete")
    api.add_resource(Get, "/get")
    app.run(debug=False)