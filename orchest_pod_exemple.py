import requests

gateway_uri = 'http://127.0.0.1:8080/'
gateway_uri_delete = 'http://127.0.0.1:8080/delete/'

def random_cnf():
    random_uri = get_image_uri('3105f5e4d004bbc2ffe607aeb4940bd66f63cfe2f0cc392ca920150163a84df5')
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
    sovler_uri = get_image_uri('87e2c0b8244c84021f28d0043aade5c6d7e9eec0467f02faf26c38e0624c7a6b')
    cnf = random_cnf()
    response = requests.post( sovler_uri+'/', params={'cnf':cnf})
    print(response.json().get('interpretation'))