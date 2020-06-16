import requests

gateway_uri = 'http://127.0.0.1:8080/'
gateway_uri_delete = 'http://127.0.0.1:8080/delete/'

def random_cnf():
    random_uri = get_image_uri('19a5867dbab12f351c75c92590779b54db94896c15106869af4174af2ea44bdc')
    response = requests.get(random_uri+'/')
    return response.json.get('cnf')

def get_image_uri(image):
    response = requests.get(gateway_uri+image)
    return response.text

if __name__ == "__main__":
    sovler_uri = get_image_uri('109edf90810021a31e5e954ce0204673c4f82d42d6208a51a55a4ff0beb24ece')
    #response = requests.get( sovler_uri+'/', params={'cnf':random_cnf()})
    print(random_cnf())