from pip._vendor import requests

gateway_port = '8080'

def random_cnf():
    pass

def get_solver_port(image):
    response = requests.get('http://localhost'+gateway_port, params={'image':image})
    return response.text()    

if __name__ == "__main__":
    sovler_port = get_solver_port('3723c39d43fc')
    response = requests.get(sovler_port, params=random_cnf())
    best_interpretation = response.text()