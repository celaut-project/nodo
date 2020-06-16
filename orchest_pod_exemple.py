import requests

gateway_uri = 'http://127.0.0.1:8080/'
gateway_uri_delete = 'http://127.0.0.1:8080/delete/'

def random_cnf():
    random_uri = get_image_uri('')
    response = requests.get(random_uri+'/')
    return response.file.get('cnf')    # cnf file

def get_image_uri(image):
    response = requests.get(gateway_uri+image)
    return response.text

if __name__ == "__main__":
    sovler_uri = get_image_uri('0e51d10fde4c0ab0126502f73a881ac7e4cf4b603965838de1cf5743134417c9')
    #response = requests.get( sovler_uri+'/', params={'cnf':random_cnf()})
    print(sovler_uri)