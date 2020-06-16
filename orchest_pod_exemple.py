import requests

gateway_uri = 'http://127.0.0.1:8080/'
gateway_uri_delete = 'http://127.0.0.1:8080/delete/'

def random_cnf():
    random_uri = get_image_uri('47dde62ca62a652d1886a0acc7f6ddab4790a698dc70f9569dd53c665465215b')
    response = requests.get(random_uri+'/')
    return response.json.get('cnf')

def get_image_uri(image):
    response = requests.get(gateway_uri+image)
    return response.text

if __name__ == "__main__":
    sovler_uri = get_image_uri('109edf90810021a31e5e954ce0204673c4f82d42d6208a51a55a4ff0beb24ece')
    #response = requests.get( sovler_uri+'/', params={'cnf':random_cnf()})
    print(random_cnf())