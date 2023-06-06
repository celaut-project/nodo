import sys, os

if __name__ == '__main__':
    if len(sys.argv) == 1:
        print("Command needed: seeder, connect or serve")
        exit()

    elif sys.argv[1] == "seeder":
        from contracts.eth_main.seeder import seed
        seed() if len(sys.argv) == 2 else seed(private_key=sys.argv[2])

    elif sys.argv[1] == 'connect':
        from src.utils.zeroconf import connect
        connect(sys.argv[2])

    elif sys.argv[1] == 'serve':
        from src.serve import serve

        if len(sys.argv) > 2 and sys.argv[2] == '--dev':
            os.environ['USE_PRINT'] = 'True'
        serve()

    elif sys.argv[1] == 'migrate':
        import os
        from database.migrate import migrate
        from contracts.eth_main.seeder import seed
        os.system("rm database.sqlite")
        migrate()
        seed() if len(sys.argv) == 2 else seed(private_key=sys.argv[2])

    elif sys.argv[1] == 'command:containers':
        from commands.containers import containers
        containers()

    elif sys.argv[1] == 'command:peers':
        from commands.peers import peers
        peers()

    elif sys.argv[1] == 'command:contracts':
        from commands.contracts import contracts
        contracts()

    else:
        print('Unknown command.')


""" > python3.10
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

"""
