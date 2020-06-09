from pip._vendor import requests

gateway_uri = 'http://0.0.0.0:8080/'
gateway_uri_delete = 'http://0.0.0.0:8080/delete/'

def random_cnf():
    return 'cnf'

def get_solver_uri(image):
    response = requests.get(gateway_uri+'3723c39d43fc')
    return response.text

if __name__ == "__main__":
    sovler_uri = get_solver_uri('3723c39d43fc')
    print(sovler_uri)
    response = requests.get( sovler_uri+random_cnf() )