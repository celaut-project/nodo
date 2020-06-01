
from pip._vendor import requests

def random_cnf():
    pass

if __name__ == "__main__":
    response = requests.get("http://localhost/", params=random_cnf())
    best_interpretation = response.text()