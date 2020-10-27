import sys

# La mejor forma de resolver el problema podria ser con redes neuronales,
#  pero un algoritmo podria salvarnos de los casos mas simples.

# Codigo de mierda.
def quizas_quisiste_decir(request, service_api):
    if type(service_api) is list:
        for elem in service_api:
            if type(elem) is dict or type(elem) is list :
                maybe_correct_response = quizas_quisiste_decir(request, elem)
                if maybe_correct_response: return maybe_correct_response
            elif type(elem) is str and request in elem:
                # if requests in elem or request_in_elem()
                return elem
    elif type(service_api) is dict:
        for elem in service_api:
            if type(elem) is dict or type(elem) is list:
                maybe_correct_response = quizas_quisiste_decir(request, elem)
                if maybe_correct_response: return maybe_correct_response
            elif type(elem) is str and request in elem:
                # if requests in elem or request_in_elem()
                return elem
        for elem in service_api.values():
            if type(elem) is dict or type(elem) is list:
                maybe_correct_response = quizas_quisiste_decir(request, elem)
                if maybe_correct_response: return maybe_correct_response
            elif type(elem) is str and request in elem:
                # if requests in elem or request_in_elem()
                return elem
            


if __name__== "__main__":

    service_api_list = [

        {
            '1':'select',
            '2':'train/start',
            '3':'train/stop',
        },

        [{
            '1':'select',
            '2':'train/start',
            '3':'train/stop',
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

    print(service_api_list)
    for service_api in service_api_list:
        correct_requests = quizas_quisiste_decir(sys.argv[1], service_api)
        print(correct_requests)