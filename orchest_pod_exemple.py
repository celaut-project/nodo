from pip._vendor import requests

gateway_uri = 'http://0.0.0.0:8080/'
gateway_uri_delete = 'http://0.0.0.0:8080/delete/'

def random_cnf():
    return 'cnf'

def get_solver_uri(image):
    response = requests.get(gateway_uri+image)
    return response.text

if __name__ == "__main__":
    sovler_uri = get_solver_uri('u5482ec44g48fc6ae17fe16bd01553623b19857209b11f5272568dacd6de619f')
    response = requests.get( sovler_uri+random_cnf() )