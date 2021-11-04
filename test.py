from threading import Lock
import utils, time,  celaut_pb2, os.path

filename = '__registry__/16426da109eed68c89bf32bcbcab208649f01d608116f1dda15e12d55fc95456'

def test():
    io = utils.IOBigData()
    with io.lock(len = 2.5*os.path.getsize(filename)):
        any = celaut_pb2.Any()
        any.ParseFromString(
            utils.read_file(filename)
        )
        print('file readed')

        service = celaut_pb2.Service()
        service.ParseFromString(any.value)

        del any

        service.container.ClearField('filesystem')
        print('filesystem clear')

    time.sleep(10)
    print('do it. ', io.get_ram_avaliable())


from threading import Thread
Thread(target=test).start()
Thread(target=test).start()