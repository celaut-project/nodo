import requests
import sys
import argparse

gateway_host = '127.0.0.1:8080'


def docker_container_id_from_name(docker_name):
    return 'token'

def check_gateway():
    return requests.get('http://'+gateway_host+'/') == 200



def launch_instance(image):
    with open('__registry__/'+image+'.json') as file:
        envs = {}
        for env in file.Container.Envs:
            print('Valor para: '+env)
            envs.update({
                env:input()
            })
    response = requests.post('http://'+gateway_host+'/'+image, json=envs)
    print(response)

def clean_cache():
    import shutil
    shutil.rmtree('/__hycache__')

def delete_instance(docker_name):
    token = docker_container_id_from_name(docker_name)
    response = requests.get('http://'+gateway_host+'/'+token)
    if response == 200: print('DO IT.')

def clean_image(image):
    print('borra la imagen de Docker.')

def images_list():
    pass

def instances_list():
    pass

def instance_output(docker_name):
    import os
    os.system(
        'sudo docker logs --follow '+docker_name
    )

if __name__ == "__main__":
    pass