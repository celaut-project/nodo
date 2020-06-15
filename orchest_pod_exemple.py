from pip._vendor import requests

gateway_uri = 'http://0.0.0.0:8080/'
gateway_uri_delete = 'http://0.0.0.0:8080/delete/'

def random_cnf():
    random_uri = get_image_uri('')
    response = requests.get(random_uri+'/')
    return response.file.get('cnf')    # cnf file

def get_image_uri(image):
    response = requests.get(gateway_uri+image)
    return response.text

if __name__ == "__main__":
    sovler_uri = get_image_uri('073b5dcc2248512fcfe0241db51a987b9f4cc7e9ea3093730435cf818acb6b4d')
    response = requests.get( sovler_uri+'/', params={'cnf':random_cnf()})