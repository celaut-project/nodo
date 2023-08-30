import threading

from main import RANDOM
from protos import celaut_pb2


def read_file(filename):
    def generator(filename):
        with open(filename, 'rb') as entry:

            for chunk in iter(lambda: entry.read(1024 * 1024), b''):

                yield chunk

    import celaut_pb2, time
    start = time.time()
    buffer = b''.join([b for b in generator(filename)])
    print(len(buffer))
    # return buffer
    any  = celaut_pb2.Any()
    any.ParseFromString(buffer)
    print(time.time() - start)

x = threading.Thread(target=read_file, args=('__registry__/'+RANDOM, ))
y = threading.Thread(target=read_file, args=('__registry__/'+RANDOM, ))
z = threading.Thread(target=read_file, args=('__registry__/'+RANDOM, ))
t = threading.Thread(target=read_file, args=('__registry__/'+RANDOM, ))

x.start()
y.start()
z.start()
t.start()

x.join()
y.join()
z.join()
t.join()