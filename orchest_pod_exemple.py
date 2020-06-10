from pip._vendor import requests

gateway_uri = 'http://0.0.0.0:8080/'
gateway_uri_delete = 'http://0.0.0.0:8080/delete/'

def random_cnf():
    random_uri = get_image_uri('67m54hg7c44g48fc6ae14hfu16bd01553623b19857209b11272568dacd684nfk4')
    response = requests.get(random_uri+'/')
    return response.file    # cnf file

def get_image_uri(image):
    response = requests.get(gateway_uri+image)
    return response.text

if __name__ == "__main__":
    sovler_uri = get_image_uri('u5482ec44g48fc6ae17fe16bd01553623b19857209b11f5272568dacd6de619f')
    response = requests.get( sovler_uri+'/', params={'cnf':random_cnf()})