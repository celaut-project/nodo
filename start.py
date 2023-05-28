import sys

if __name__ == '__main__':
    if len(sys.argv) == 1:
        print("Command needed: seeder, connect or serve")
        exit()

    match sys.argv[1]:

        case "seeder":
            from contracts.eth_main.seeder import seed
            seed() if len(sys.argv) == 2 else seed(private_key=sys.argv[2])

        case 'connect':
            from src.utils.zeroconf import connect
            connect(sys.argv[2])

        case 'serve':
            from src.serve import serve
            serve()

        case other:
            print('Unknown command.')

