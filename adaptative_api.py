import sys

class string(str):
    def like(self, other: str) -> bool:
        import collections
        a = self.lower()
        b = other.lower()
        return a in b or b in a


# La mejor forma de resolver el problema podria ser con redes neuronales,
#  pero un algoritmo podria salvarnos de los casos mas simples.

# Codigo de mierda.
def quizas_quisiste_decir(request: str, service_api: dict or list) -> str:
    responses = []
    if type(service_api) is list:
        for elem in service_api:
            if type(elem) is dict or type(elem) is list :
                r = quizas_quisiste_decir(request, elem)
                if type(r) is str:
                    responses.append(r)
                elif type(r) is list:
                    responses += r
            elif type(elem) is str and string(request).like(elem) :
                return elem
    elif type(service_api) is dict:
        for elem in service_api:
            if type(elem) is dict or type(elem) is list:
                r = quizas_quisiste_decir(request, elem)
                if type(r) is str:
                    responses.append(r)
                elif type(r) is list:
                    responses += r
            elif type(elem) is str and string(request).like(elem) :
                responses.append(elem)
        for elem in service_api.values():
            if type(elem) is dict or type(elem) is list:
                r = quizas_quisiste_decir(request, elem)
                if type(r) is str:
                    responses.append(r)
                elif type(r) is list:
                    responses += r
            elif type(elem) is str and string(request).like(elem) :
                responses.append(elem)
    return responses


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

    for service_api in service_api_list:
        correct_request = quizas_quisiste_decir(sys.argv[1], service_api)
        print(correct_request)
