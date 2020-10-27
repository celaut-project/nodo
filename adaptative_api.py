#La mejor forma de resolver el problema podria ser con redes neuronales,
#  pero un algoritmo podria salvarnos de los casos mas simples.

def quizas_quisiste_decir(request, service_api):
    if type(service_api) is list:
        for elem in service_api:
            if type(elem) is str:
                maybe_correct_response = quizas_quisiste_decir(request, elem)
                if maybe_correct_response: return maybe_correct_response
            if request in elem:
                return elem
    elif type(service_api) is dict:
        for elem

def __main__():
    request = 'start'

    service_api_list = [

        {
            1:'select'
            2:'train/start'
            3:'train/stop'
        },

        [{
            1:'select'
            2:'train/start'
            3:'train/stop'
        }],

        [
            'select',
            'train/start',
            'train/stop'
        ],

        {
            'select': 'selecciona el mejor resultado',
            'train':{
                'start': 'comienza el entrenamiento',
                'stop': 'paraliza el entrenamiento'
            }
        }

    ]

    for service_api in service_api_list:
        correct_requests = quizas_quisiste_decir(request, service_api)
        print(correct_requests)