import utils, time, celaut_pb2, gc

start = time.time()
buff = utils.read_file('__registry__/16426da109eed68c89bf32bcbcab208649f01d608116f1dda15e12d55fc95456')
any = celaut_pb2.Any()
any.ParseFromString(buff)
print(len(buff), time.time() - start)
del buff