import utils, time,  celaut_pb2

any = celaut_pb2.Any()
any.ParseFromString(
    utils.read_file('__registry__/16426da109eed68c89bf32bcbcab208649f01d608116f1dda15e12d55fc95456')
    )

utils.wait_to_prevent_kill(len(any.value))
service = celaut_pb2.Service()
service.ParseFromString(any.value)
del any
service.container.ClearField('filesystem')
print(service)
time.sleep(10)
print('do it.')