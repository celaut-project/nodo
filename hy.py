GATEWAY_HOST = '127.0.0.1:8080'

def docker_container_id_from_name(docker_name):
    return 'token'

def check_gateway():
    import requests
    try:
        return requests.get('http://'+GATEWAY_HOST+'/') == 200
    except ConnectionError:
        return False



def launch_instance(image):
    import requests
    with open('__registry__/'+image+'.json') as file:
        envs = {}
        for env in file.Container.Envs:
            print('Valor para: '+env)
            envs.update({
                env:input()
            })
    response = requests.post('http://'+GATEWAY_HOST+'/'+image, json=envs)
    print(response)

def clean_cache():
    import os
    os.system('rm -rf __hycache__')
    os.system('mkdir __hycache__')
    print('Cleaned.')

def delete_instance(docker_name):
    import requests
    token = docker_container_id_from_name(docker_name)
    print('Confirm to delete '+docker_name+' [Yes/No] ')
    inpt = input()
    if inpt == 'Y' or inpt == 'y' or inpt == 'yes' or inpt == 'Yes':
        if requests.get('http://'+GATEWAY_HOST+'/'+token) == 200: print('DO IT.')
    else: print('Canceled.')

def clean_image(image):
    print('borra la imagen de Docker.')

def images_list():
    import os
    for l in os.listdir('__registry__'):
        if len(l.split('.'))==1:
            print(l)


def instances_list():
    pass

def instance_output(docker_name):
    import os
    os.system(
        'sudo docker logs --follow '+docker_name
    )

if __name__ == "__main__":
    import os
    os.chdir( os.getcwd() )
    os.system('python3')