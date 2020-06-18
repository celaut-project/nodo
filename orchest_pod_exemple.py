import requests

gateway_uri = 'http://127.0.0.1:8080/'
gateway_uri_delete = 'http://127.0.0.1:8080/delete/'

def random_cnf():
    random_uri = get_image_uri('f2aaf326eabb22b1775d0fbd91d9a7660394ca300bef3f4d63df6f955d60e0dd')
    docker_snail = True
    while docker_snail==True:
        try:
            response = requests.get(random_uri+'/')
            docker_snail = False
            if response.status_code != 200:
                print("Algo va mal ....", response)
                exit()
        except requests.exceptions.ConnectionError:
            print('Docker va muy lento.....')
    cnf = response.json().get('cnf')
    print(cnf)
    return cnf

def get_image_uri(image):
    response = requests.get(gateway_uri+image)
    return response.text

if __name__ == "__main__":
    sovler_uri = get_image_uri('1f3e8ef0742e4c779a1d8cf0cabd05ab89796816edd5b867bc28d0b5955fef62')
    cnf = random_cnf()
    response = requests.post( sovler_uri+'/', json={'cnf':cnf})
    print(response.json().get('interpretation'))