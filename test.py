import utils, time, celaut_pb2

def read_file(filename) -> bytes:
    def generator(filename):
        with open(filename, 'rb') as entry:
            for chunk in iter(lambda: entry.read(1024 * 1024), b''):
                    yield chunk
    return b''.join([b for b in generator(filename)])

start = time.time()
buff = utils.read_file('__registry__/16426da109eed68c89bf32bcbcab208649f01d608116f1dda15e12d55fc95456')
any = celaut_pb2.Any()
any.ParseFromString(buff)
print(len(buff), time.time() - start)

start = time.time()
print(
    len(read_file('__registry__/16426da109eed68c89bf32bcbcab208649f01d608116f1dda15e12d55fc95456')), time.time() - start
)