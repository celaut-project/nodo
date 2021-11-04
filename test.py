import utils, time,  celaut_pb2, os.path

filename = '__registry__/16426da109eed68c89bf32bcbcab208649f01d608116f1dda15e12d55fc95456'

io = utils.IOBigData()
io.lock_ram(os.path.getsize(filename))

any = celaut_pb2.Any()
any.ParseFromString(
    utils.read_file(filename)
    )

io.lock_ram(len(any.value))
service = celaut_pb2.Service()
service.ParseFromString(any.value)

del any
io.unlock_ram(os.path.getsize(filename))


filesys_len = len(service.container.filesystem)
service.container.ClearField('filesystem')
print('filesystem clear')

io.unlock_ram(filesys_len)
time.sleep(10)
print('do it. ', io.get_ram_avaliable())