from pip._vendor import requests

gateway_port = 'http://0.0.0.0:8080'

def random_cnf():
    pass

def get_solver_port(image):
    response = requests.get(gateway_port, params={'image':image})
    return response

if __name__ == "__main__":
    sovler_port = get_solver_port('3723c39d43fc')
    print(sovler_port)
    #response = requests.get(sovler_port, params={'cnf':random_cnf()})